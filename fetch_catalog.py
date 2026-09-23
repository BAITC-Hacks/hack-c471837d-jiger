import argparse
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app import redact_secrets
from app.catalog import CATALOG_PATH

API_URL = "https://ekt.kz/api/products"
MAX_RETRIES = 3


def unexpected_response(payload: object) -> RuntimeError:
    sample = redact_secrets(json.dumps(payload, ensure_ascii=False, indent=2))
    return RuntimeError(
        "Неожиданная структура ответа API. Уточните формат полей перед загрузкой.\n"
        f"Пример сырого ответа (до 2000 символов):\n{sample[:2000]}"
    )


def request_json(client: httpx.Client, url: str, params: dict | None = None) -> object:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
        except httpx.TimeoutException:
            reason = "API не ответил за 15 секунд"
        except httpx.RequestError:
            reason = "не удалось соединиться с API"
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status in (401, 403):
                raise RuntimeError(
                    f"API отклонил доступ (HTTP {status}). "
                    "Проверьте EKT_API_USER и EKT_API_PASSWORD в .env."
                ) from None
            reason = f"API вернул HTTP {status}"
            if status not in (408, 429) and status < 500:
                raise RuntimeError(reason) from None
        else:
            try:
                return response.json()
            except ValueError:
                raise unexpected_response(response.text) from None

        if attempt == MAX_RETRIES:
            raise RuntimeError(
                f"{reason}; исчерпаны {MAX_RETRIES} повторных попытки."
            ) from None
        print(f"{reason}. Повтор {attempt + 1}/{MAX_RETRIES}…", file=sys.stderr)
        time.sleep(attempt + 1)


def normalize_product(item: dict, detail: object) -> dict:
    if not isinstance(detail, dict) or detail.get("id") != item["id"]:
        raise unexpected_response(detail)
    if not isinstance(detail.get("name"), str) or not detail["name"].strip():
        raise unexpected_response(detail)
    if "price" not in detail or (
        detail["price"] is not None and type(detail["price"]) not in (int, float, str)
    ):
        raise unexpected_response(detail)
    if "detail" in item:
        raise unexpected_response(item)
    # Ответы могут различаться даже в поле image: сохраняем оба без изменений.
    return {**item, "detail": detail}


def fetch_product(client: httpx.Client, item: dict) -> dict:
    link = item.get("url_api_detail")
    if not isinstance(link, str) or not link:
        raise unexpected_response(item)
    try:
        url = httpx.URL(link)
    except httpx.InvalidURL:
        raise unexpected_response(item) from None
    # Basic Auth разрешён только для исходного сервера EKT.
    if url.scheme != "https" or url.host != "ekt.kz" or url.port not in (None, 443):
        raise unexpected_response(item)
    detail = request_json(client, link)
    return normalize_product(item, detail)


def fetch_catalog(client: httpx.Client, max_pages: int) -> tuple[list[dict], int]:
    products = []
    seen_ids = set()
    pages = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for page in range(1, max_pages + 1):
            payload = request_json(client, API_URL, {"page": page})
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise unexpected_response(payload)
            items = payload["items"]
            if not items:
                print(f"Страница {page} пустая; загрузка завершена.")
                break
            pending = []
            for item in items:
                if not isinstance(item, dict) or type(item.get("id")) not in (int, str):
                    raise unexpected_response(item)
                if item["id"] not in seen_ids:
                    pending.append(item)
                    seen_ids.add(item["id"])
            products.extend(pool.map(lambda item: fetch_product(client, item), pending))
            pages += 1
            print(f"Страница {page}: {len(items)} позиций; собрано {len(products)} товаров.", flush=True)
        else:
            print(f"Достигнут лимит MAX_PAGES={max_pages}; каталог может быть неполным.")
    return products, pages


def save_catalog(products: list[dict]) -> None:
    temporary_path = None
    try:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=CATALOG_PATH.parent,
            prefix=".products-", suffix=".tmp", delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(products, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary_path.replace(CATALOG_PATH)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("MAX_PAGES должен быть целым числом.") from None
    if number < 1:
        raise argparse.ArgumentTypeError("MAX_PAGES должен быть больше нуля.")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузить реальный каталог EKT в JSON.")
    parser.add_argument("--max-pages", type=positive_int, default=20, metavar="MAX_PAGES")
    args = parser.parse_args()
    try:
        load_dotenv(Path(__file__).resolve().parent / ".env")
        user = os.environ.get("EKT_API_USER")
        password = os.environ.get("EKT_API_PASSWORD")
        if not user or not password:
            raise RuntimeError("Укажите EKT_API_USER и EKT_API_PASSWORD в окружении или .env.")
        with httpx.Client(auth=httpx.BasicAuth(user, password), timeout=15.0) as client:
            products, pages = fetch_catalog(client, args.max_pages)
        if not products:
            raise RuntimeError("API вернул пустой каталог; новый кэш не записан.")
        save_catalog(products)
    except RuntimeError as error:
        print(redact_secrets(f"Ошибка: {error}"), file=sys.stderr)
    except (OSError, UnicodeError):
        print("Не удалось прочитать .env или записать каталог. Проверьте права доступа и UTF-8.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Загрузка прервана.", file=sys.stderr)
    else:
        print(f"Сохранено {len(products)} товаров с {pages} страниц в data/products.json.")
        return 0

    if CATALOG_PATH.exists():
        print("Старый кэш data/products.json оставлен без изменений.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
