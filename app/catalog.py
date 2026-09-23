import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"
PRODUCT_FIELDS = ("id", "name", "price", "category", "description", "characteristics")


def load_catalog() -> list[dict]:
    try:
        with CATALOG_PATH.open(encoding="utf-8") as file:
            products = json.load(file)
    except FileNotFoundError:
        raise RuntimeError(
            "Каталог data/products.json не найден. Запустите python fetch_catalog.py."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(
            "Каталог data/products.json повреждён. Повторите python fetch_catalog.py."
        ) from None
    except OSError:
        raise RuntimeError(
            "Не удалось прочитать data/products.json. Проверьте права доступа."
        ) from None

    if not isinstance(products, list) or any(
        not isinstance(product, dict) for product in products
    ):
        raise RuntimeError("Неверный формат каталога: ожидается список товаров.")
    return products


def search_products(query: str, max_results: int = 5) -> list[dict]:
    if not isinstance(query, str):
        raise ValueError("Поисковый запрос должен быть строкой.")
    if type(max_results) is not int or max_results < 1:
        raise ValueError("Количество результатов должно быть целым числом больше нуля.")

    words = set(re.findall(r"\w+", query.casefold()))
    if not words:
        return []

    matches = []
    for product in load_catalog():
        searchable = " ".join(
            str(product.get(field) or "")
            for field in ("name", "category", "description", "characteristics")
        ).casefold()
        score = sum(word in searchable for word in words)
        if score:
            matches.append((score, product))

    matches.sort(key=lambda match: match[0], reverse=True)
    return [
        {field: product[field] for field in PRODUCT_FIELDS if field in product}
        for _, product in matches[:max_results]
    ]
