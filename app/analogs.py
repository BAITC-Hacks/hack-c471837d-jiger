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


_FAMILIES = (
    ("rcbo", r"диф\.?\s*авт|дифференциальн\w*\s+автомат|авдт"),
    ("rcd", r"\bузо\b|выключатель\s+дифференциальн"),
    ("breaker", r"автомат|\bав\b"),
    ("socket", r"розетк"), ("switch", r"выключател"),
    ("relay", r"реле"), ("contactor", r"контактор"),
    ("light", r"светильник|\bled\b"), ("lamp", r"ламп"),
    ("terminal", r"клемм|\bсиз\b"), ("coupling", r"муфт"),
    ("extension", r"удлинител|сетевой\s+фильтр"),
    ("box", r"коробк"), ("cabinet", r"щит|шкаф"),
)
_UNITS = {
    "ма": ("ток утечки", "мА"), "ma": ("ток утечки", "мА"),
    "а": ("ток", "А"), "a": ("ток", "А"),
    "ка": ("отключающая способность", "кА"), "ka": ("отключающая способность", "кА"),
    "в": ("напряжение", "В"), "v": ("напряжение", "В"),
    "вт": ("мощность", "Вт"), "w": ("мощность", "Вт"),
    "лм": ("световой поток", "лм"), "lm": ("световой поток", "лм"),
    "к": ("цветовая температура", "К"), "k": ("цветовая температура", "К"),
}
_MEASUREMENTS = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(ка|ka|ма|ma|вт|лм|lm|а|a|в|v|w|к|k)(?![a-zа-я])",
    re.IGNORECASE,
)


def _family(product: dict) -> str:
    name = str(product.get("name") or "").casefold()
    return next((family for family, pattern in _FAMILIES if re.search(pattern, name)), "")


def _specs(product: dict) -> dict[str, set[str]]:
    """Only compare parameters explicitly present in the source text."""
    text = " ".join(str(product.get(field) or "") for field in (
        "name", "description", "characteristics",
    )).casefold()
    result: dict[str, set[str]] = {}
    for number, unit in _MEASUREMENTS.findall(text):
        label, normalized_unit = _UNITS[unit]
        result.setdefault(label, set()).add(f"{number.replace(',', '.')} {normalized_unit}")
    for label, pattern in (
        ("защита", r"\bip\s*(\d{2})\b"),
        ("полюса", r"\b([1-4]\s*[pр](?:\s*\+\s*n)?)\b"),
    ):
        values = {re.sub(r"\s+", "", value).replace("р", "p").upper() for value in re.findall(pattern, text)}
        if values:
            result[label] = {"IP" + value for value in values} if label == "защита" else values
    return result


def _category_match(source: dict, candidate: dict) -> tuple[str, bool] | None:
    source_category, candidate_category = _category(source), _category(candidate)
    if source_category and candidate_category:
        return (source_category, False) if source_category.casefold() == candidate_category.casefold() else None
    source_path = catalog.category_path(source)
    candidate_path = catalog.category_path(candidate)
    # A full matching path is more precise than a shared top-level department.
    if len(source_path) < 2 or [x.casefold() for x in source_path] != [x.casefold() for x in candidate_path]:
        return None
    return (" / ".join(source_path), True)


def find_analogs(sku: str) -> list[dict]:
    """Return up to five stocked candidates, with explicitly unverified compatibility.

    SKU matching is exact (ignoring surrounding spaces and case), including the
    supplier articles in the cache. Results contain normalized product fields and
    a factual ``reason``. Missing KATEGORIYA falls back to the full catalog URL
    category plus name similarity; matching a department alone is insufficient.
    """
    if not isinstance(sku, str) or not sku.strip():
        raise ValueError("Для подбора аналогов нужен непустой артикул.")

    products = catalog.load_catalog()
    # Индекс артикулов и псевдонимов: точное совпадение за O(1).
    source = catalog.find_by_article(sku)
    if source is None:
        return []
    source_fields = catalog.product_fields(source)
    source_features = _features(source_fields)
    source_specs = _specs(source_fields)
    source_family = _family(source_fields)
    source_name = set(re.findall(r"[a-zа-яё]{3,}", str(source_fields.get("name") or "").casefold()))
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
        category_match = _category_match(source_fields, fields)
        if category_match is None:
            continue
        category, fallback = category_match
        family = _family(fields)
        if source_family and family and source_family != family:
            continue
        candidate_name = set(re.findall(r"[a-zа-яё]{3,}", str(fields.get("name") or "").casefold()))
        shared_name = source_name & candidate_name
        name_union = source_name | candidate_name
        name_similarity = len(shared_name) / len(name_union) if name_union else 0
        if fallback and (not shared_name or name_similarity < 0.2):
            continue
        articles = catalog.product_articles(product)
        if str(product_id) in seen_ids or articles & seen_articles:
            continue
        features = _features(fields)
        union = source_features | features
        similarity = len(source_features & features) / len(union) if union else 0
        specs = _specs(fields)
        # A different pole layout or explicitly disjoint voltage values are
        # not a useful replacement candidate, even in the same catalog section.
        if any(
            key in source_specs and key in specs and source_specs[key].isdisjoint(specs[key])
            for key in ("полюса", "напряжение")
        ):
            continue
        shared_specs = source_specs.keys() & specs.keys()
        matched = sorted(key for key in shared_specs if source_specs[key] == specs[key])
        different = sorted(key for key in shared_specs if source_specs[key] != specs[key])
        # Technical values outrank shared advertising text or a shared brand.
        score = 3 * len(matched) - 2 * len(different) + name_similarity + similarity
        reason = f"Тот же раздел «{category}»; в наличии {quantity} шт. "
        if matched:
            reason += "Совпадают: " + "; ".join(
                f"{key}: {', '.join(sorted(specs[key]))}" for key in matched
            ) + ". "
        if different:
            reason += "Различаются: " + "; ".join(
                f"{key}: {', '.join(sorted(source_specs[key]))} → {', '.join(sorted(specs[key]))}"
                for key in different
            ) + ". "
        reason += "Совместимость не подтверждена: перед заменой сравните все характеристики и условия установки."
        candidates.append((score, fields, articles, reason))

    candidates.sort(key=lambda entry: (
        -entry[0], entry[1]["article"].casefold(), str(entry[1]["id"]),
    ))
    results = []
    for _, fields, articles, reason in candidates:
        product_id = str(fields["id"])
        if product_id in seen_ids or articles & seen_articles:
            continue
        seen_ids.add(product_id)
        seen_articles.update(articles)
        results.append({
            **fields,
            "reason": reason,
            "compatibility_verified": False,
        })
        if len(results) == MAX_ANALOGS:
            break
    return results
