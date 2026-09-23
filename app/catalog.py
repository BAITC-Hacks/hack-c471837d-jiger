import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"
PRODUCT_FIELDS = (
    "id", "name", "article", "price", "category", "image", "url", "url_api_detail",
    "description", "quantity", "properties", "characteristics",
)
SEARCH_TOKEN = re.compile(r"\w+(?:[-./]\w+)*")
MEASUREMENT = re.compile(r"(?<![\w./-])(\d+(?:[.,]\d+)?)\s*(к?вт|м?а|к?в|мм|см|м|лм|к|гц)\b")


def _search_text(text: str) -> str:
    # Catalog descriptions use both "30мА" and "30 мА". Do not normalize inside SKUs.
    return MEASUREMENT.sub(lambda match: match[1].replace(",", ".") + match[2], text.casefold())


def product_fields(product: dict) -> dict:
    detail = product.get("detail", {})
    # Поля списка остаются исходными; отсутствующие берём из полной карточки.
    return {
        field: product[field] if field in product else detail[field]
        for field in PRODUCT_FIELDS
        if field in product or field in detail
    }


def product_articles(product: dict) -> set[str]:
    """Read article aliases without dropping leading zeroes or separators."""
    articles = set()
    for source in (product, product.get("detail", {})):
        if not isinstance(source, dict):
            continue
        values = [source.get("article")]
        properties = source.get("properties")
        if isinstance(properties, dict):
            values.extend(properties.get(key) for key in ("CML2_ARTICLE", "ARTIKULPOSTAVSHCHIKA"))
        articles.update(str(value).casefold().strip() for value in values if value is not None)
    return articles - {""}


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
    # Keep whole identifiers: splitting TEST-LED-30-A-MISSING would match TEST-LED-30-A.
    article_words = set(SEARCH_TOKEN.findall(folded_query))
    words = set(SEARCH_TOKEN.findall(_search_text(folded_query)))
    if not words:
        return []
    identifiers = {word for word in words if not word.isalpha()}

    matches = []
    for product in load_catalog():
        sources = (product, product.get("detail", {}))
        searchable = " ".join(
            str(source.get(field) or "")
            for source in sources
            for field in ("article", "name", "category", "description", "characteristics", "properties")
        ).casefold()
        searchable_words = set(SEARCH_TOKEN.findall(searchable))
        searchable = _search_text(searchable)
        searchable_words.update(SEARCH_TOKEN.findall(searchable))
        articles = product_articles(product)
        exact_article = folded_query in articles or bool(articles & article_words)
        # Unknown codes must not fall back to a coincidentally matching name/category.
        # Whole numeric/specification tokens still allow queries such as "IP20" or "30 Вт".
        # An explicit known article still identifies the product when the buyer adds
        # an order quantity or a requested specification that needs checking.
        if not exact_article and not identifiers.issubset(searchable_words):
            continue
        score = sum(word in searchable for word in words)
        if exact_article or score:
            matches.append((exact_article, score, product))

    matches.sort(key=lambda match: (match[0], match[1]), reverse=True)
    return [
        product_fields(product)
        for _, _, product in matches[:max_results]
    ]
