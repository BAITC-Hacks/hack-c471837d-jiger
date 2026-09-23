"""Must have 2: offline contract tests for alternatives to every zero-stock SKU.

Contract: app.agent.find_analogs(sku) returns a list of product dictionaries
with id, article, quantity, properties.KATEGORIYA and a nonempty reason string.

Install: python -m pip install pytest pytest-asyncio respx
Run: python -m pytest tests/test_tools_analogs.py
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app import agent
from app import catalog as product_catalog

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _out_of_stock_cases() -> list:
    products = json.loads((FIXTURES_DIR / "catalog_small.json").read_text(encoding="utf-8"))
    cases = [
        pytest.param(product["article"], id=product["article"])
        for product in products
        if product["quantity"] == 0
    ]
    assert cases, "В catalog_small.json должны быть товары с нулевым остатком."
    return cases


# Direct parametrization covers all zero-stock articles, not just the first fixture SKU.
@pytest.mark.parametrize("out_of_stock_sku", _out_of_stock_cases())
@pytest.mark.usefixtures("offline_catalog")
def test_out_of_stock_sku_has_valid_analogs(catalog: list[dict], out_of_stock_sku: str) -> None:
    by_article = {product["article"]: product for product in catalog}
    source = by_article[out_of_stock_sku]
    assert source["quantity"] == 0
    source_category = source["properties"]["KATEGORIYA"]
    assert source_category

    analogs = agent.find_analogs(out_of_stock_sku)

    assert isinstance(analogs, list), "Инструмент должен возвращать список аналогов."
    assert analogs, f"Для {out_of_stock_sku} должен быть минимум один аналог."
    seen_articles: set[str] = set()
    seen_ids: set[int] = set()
    for analog in analogs:
        assert isinstance(analog, dict), "Каждый аналог должен быть объектом товара."
        article = analog.get("article")
        assert isinstance(article, str) and article in by_article, "Аналог отсутствует в каталоге."
        expected = by_article[article]

        assert article != out_of_stock_sku, "Исходный артикул включён в собственные аналоги."
        assert analog.get("id") == expected["id"]
        assert analog["id"] != source["id"], "Исходный товар включён в собственные аналоги."
        assert article not in seen_articles, f"Артикул {article} повторяется в списке аналогов."
        assert analog["id"] not in seen_ids, f"ID {analog['id']} повторяется в списке аналогов."
        seen_articles.add(article)
        seen_ids.add(analog["id"])

        quantity = analog.get("quantity")
        assert type(quantity) in (int, float) and quantity > 0, f"У {article} нет положительного остатка."
        assert expected["quantity"] > 0, f"В исходном каталоге {article} отсутствует в наличии."
        assert quantity == expected["quantity"], f"Остаток {article} не совпадает с каталогом."

        reason = analog.get("reason")
        assert isinstance(reason, str) and reason.strip(), f"Для {article} нет обоснования."

        properties = analog.get("properties")
        assert isinstance(properties, dict), f"У {article} нет свойств с категорией."
        assert properties.get("KATEGORIYA") == source_category, f"У {article} другая категория."
        assert expected["properties"]["KATEGORIYA"] == source_category, (
            f"Категория {article} в исходном каталоге не совпадает с категорией {out_of_stock_sku}."
        )


@pytest.mark.usefixtures("offline_catalog")
def test_analogs_are_available_through_agent_dispatch(
    catalog: list[dict], out_of_stock_sku: str,
) -> None:
    assert any(tool["function"]["name"] == "find_analogs" for tool in agent.TOOLS)
    result = agent.execute_tool("find_analogs", json.dumps({"sku": out_of_stock_sku}))
    source = next(product for product in catalog if product["article"] == out_of_stock_sku)
    by_id = {product["id"]: product for product in catalog}
    assert isinstance(result, list) and result
    for product in result:
        expected = by_id[product["id"]]
        assert product["article"] == expected["article"] != source["article"]
        assert product["quantity"] == expected["quantity"] > 0
        assert product["properties"]["KATEGORIYA"] == source["properties"]["KATEGORIYA"]
        assert isinstance(product["reason"], str) and product["reason"].strip()


@pytest.mark.usefixtures("offline_catalog")
def test_unknown_sku_does_not_use_partial_matches(out_of_stock_sku: str) -> None:
    assert agent.find_analogs(f"{out_of_stock_sku}-MISSING") == []
    assert agent.find_analogs(out_of_stock_sku.rsplit("-", 1)[0]) == []


@pytest.mark.usefixtures("offline_catalog")
def test_sku_case_whitespace_and_supplier_article(out_of_stock_sku: str) -> None:
    expected = agent.find_analogs(out_of_stock_sku)
    assert expected
    assert agent.find_analogs(f"  {out_of_stock_sku.lower()}  ") == expected

    products = product_catalog.load_catalog()
    source = next(product for product in products if product["article"] == out_of_stock_sku)
    source["detail"]["properties"]["ARTIKULPOSTAVSHCHIKA"] = "SUPPLIER-001"
    product_catalog.CATALOG_PATH.write_text(json.dumps(products), encoding="utf-8")
    assert agent.find_analogs("supplier-001") == expected


@pytest.mark.usefixtures("no_openai")
@pytest.mark.parametrize("sku", [None, 123, True, "", "  "])
def test_invalid_sku_is_rejected_without_openai(sku: object) -> None:
    with pytest.raises(ValueError, match="артикул"):
        agent.find_analogs(sku)
    result = agent.execute_tool("find_analogs", json.dumps({"sku": sku}))
    assert isinstance(result, dict) and set(result) == {"error"}
    assert "артикул" in result["error"]


@pytest.mark.usefixtures("offline_catalog")
@pytest.mark.parametrize("quantity", [0, -1, True, "2", None, float("inf"), float("nan")])
def test_unavailable_or_invalid_stock_is_never_recommended(
    out_of_stock_sku: str, quantity: object,
) -> None:
    products = product_catalog.load_catalog()
    for product in products:
        if product["article"] != out_of_stock_sku:
            product["detail"]["quantity"] = quantity
    product_catalog.CATALOG_PATH.write_text(json.dumps(products), encoding="utf-8")
    assert agent.find_analogs(out_of_stock_sku) == []


@pytest.mark.usefixtures("offline_catalog")
def test_analogs_deduplicate_ids_and_articles(out_of_stock_sku: str) -> None:
    products = product_catalog.load_catalog()
    source = next(product for product in products if product["article"] == out_of_stock_sku)
    category = source["detail"]["properties"]["KATEGORIYA"]
    candidate = next(product for product in products if (
        product["detail"]["quantity"] > 0
        and product["detail"]["properties"]["KATEGORIYA"] == category
    ))
    same_id = deepcopy(candidate)
    same_id["article"] = same_id["detail"]["article"] = "ZZZ-DUPLICATE"
    same_id["detail"]["properties"]["CML2_ARTICLE"] = "ZZZ-DUPLICATE"
    same_id["detail"]["properties"]["ARTIKULPOSTAVSHCHIKA"] = "ZZZ-DUPLICATE"
    same_article = deepcopy(candidate)
    same_article["id"] = same_article["detail"]["id"] = 999999
    product_catalog.CATALOG_PATH.write_text(
        json.dumps([source, candidate, same_id, same_article]), encoding="utf-8",
    )
    results = agent.find_analogs(out_of_stock_sku)
    assert len(results) == 1
    assert results[0]["id"] == candidate["id"]
    assert results[0]["article"] == candidate["article"]


@pytest.mark.usefixtures("offline_catalog")
def test_unknown_source_category_does_not_produce_analogs(out_of_stock_sku: str) -> None:
    products = product_catalog.load_catalog()
    for product in products:
        product["detail"]["properties"].pop("KATEGORIYA", None)
    product_catalog.CATALOG_PATH.write_text(json.dumps(products), encoding="utf-8")
    assert agent.find_analogs(out_of_stock_sku) == []
