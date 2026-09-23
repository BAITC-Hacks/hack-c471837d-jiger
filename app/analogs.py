"""Deterministic alternatives from the local catalog, with no external calls."""

import math
import re

from app import catalog

MAX_ANALOGS = 5


def _category(product: dict) -> str:
    properties = product.get("properties")
    value = properties.get("KATEGORIYA") if isinstance(properties, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _features(product: dict) -> set[str]:
    # Numbers and dimensions stay in the comparison. Shared text ranks candidates;
    # it does not establish electrical or mechanical interchangeability.
    text = " ".join(str(product.get(field) or "") for field in (
        "name", "description", "characteristics",
    ))
    return set(re.findall(r"\w+", text.casefold()))


def find_analogs(sku: str) -> list[dict]:
    """Return up to five stocked alternatives in the exact source category.

    SKU matching is exact (ignoring surrounding spaces and case), including the
    supplier articles in the cache. Results contain normalized product fields and
    a factual ``reason``. Unknown articles or categories yield an empty list.
    """
    if not isinstance(sku, str) or not sku.strip():
        raise ValueError("Для подбора аналогов нужен непустой артикул.")

    products = catalog.load_catalog()
    # Индекс артикулов и псевдонимов: точное совпадение за O(1).
    source = catalog.find_by_article(sku)
    if source is None:
        return []
    source_fields = catalog.product_fields(source)
    category = _category(source_fields)
    if not category:
        return []

    source_features = _features(source_fields)
    seen_ids = {str(source_fields.get("id"))}
    seen_articles = catalog.product_articles(source)
    candidates = []
    for product in products:
        fields = catalog.product_fields(product)
        product_id = fields.get("id")
        article = fields.get("article")
        quantity = fields.get("quantity")
        if type(product_id) not in (int, str) or not str(product_id).strip():
            continue
        if not isinstance(article, str) or not article.strip():
            continue
        if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity <= 0:
            continue
        if _category(fields).casefold() != category.casefold():
            continue
        articles = catalog.product_articles(product)
        if str(product_id) in seen_ids or articles & seen_articles:
            continue
        features = _features(fields)
        union = source_features | features
        similarity = len(source_features & features) / len(union) if union else 0
        candidates.append((similarity, fields, articles))

    candidates.sort(key=lambda entry: (
        -entry[0], entry[1]["article"].casefold(), str(entry[1]["id"]),
    ))
    results = []
    for _, fields, articles in candidates:
        product_id = str(fields["id"])
        if product_id in seen_ids or articles & seen_articles:
            continue
        seen_ids.add(product_id)
        seen_articles.update(articles)
        results.append({
            **fields,
            "reason": (
                f"Та же категория «{_category(fields)}»; в наличии {fields['quantity']} шт. "
                "Перед заменой сравните характеристики и требования к совместимости."
            ),
        })
        if len(results) == MAX_ANALOGS:
            break
    return results
