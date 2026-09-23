import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"
PRODUCT_FIELDS = (
    "id", "name", "article", "price", "category", "image", "url", "url_api_detail",
    "description", "quantity", "properties", "characteristics",
)


def product_fields(product: dict) -> dict:
    detail = product.get("detail", {})
    # Поля списка остаются исходными; отсутствующие берём из полной карточки.
    return {
        field: product[field] if field in product else detail[field]
        for field in PRODUCT_FIELDS
        if field in product or field in detail
    }


def load_catalog() -> list[dict]:
    try:
        with CATALOG_PATH.open(encoding="utf-8") as file:
            products = json.load(file)
    except FileNotFoundError:
        raise RuntimeError(
            "Каталог data/products.json не найден: запустите python fetch_catalog.py "
            "(с uv: uv run python fetch_catalog.py)."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(
            "Каталог data/products.json повреждён. Повторите uv run python fetch_catalog.py."
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

    folded_query = query.casefold().strip()
    words = set(re.findall(r"\w+", folded_query))
    if not words:
        return []

    matches = []
    for product in load_catalog():
        sources = (product, product.get("detail", {}))
        searchable = " ".join(
            str(source.get(field) or "")
            for source in sources
            for field in ("article", "name", "category", "description", "characteristics", "properties")
        ).casefold()
        articles = []
        for source in sources:
            articles.append(str(source.get("article") or "").casefold().strip())
            properties = source.get("properties")
            if isinstance(properties, dict):
                articles.extend(
                    str(properties.get(key) or "").casefold().strip()
                    for key in ("CML2_ARTICLE", "ARTIKULPOSTAVSHCHIKA")
                )
        exact_article = any(
            article and re.search(r"(?<!\w)" + re.escape(article) + r"(?!\w)", folded_query)
            for article in articles
        )
        score = sum(word in searchable for word in words)
        if exact_article or score:
            matches.append((exact_article, score, product))

    matches.sort(key=lambda match: (match[0], match[1]), reverse=True)
    return [
        product_fields(product)
        for _, _, product in matches[:max_results]
    ]
