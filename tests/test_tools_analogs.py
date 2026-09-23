"""Must have 2: offline contract tests for alternatives to every zero-stock SKU.

Expected contract: app.agent.find_analogs(sku) returns a list of product dictionaries
with id, article, quantity, properties.KATEGORIYA and a nonempty reason string.
This tool does not yet exist in app/: absence is a test failure, not a skip.
Prerequisite: implement Must have 2 in app/ and register the agent tool in TOOLS
and execute_tool. The contract above is the existing test requirement, not an
implemented API; adapt the call and field locations once a real contract exists.

Install: python -m pip install pytest pytest-asyncio respx
Run: python -m pytest tests/test_tools_analogs.py
"""

import json
from pathlib import Path

import pytest

from app import agent

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
pytestmark = pytest.mark.usefixtures("offline_catalog")


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
def test_out_of_stock_sku_has_valid_analogs(catalog: list[dict], out_of_stock_sku: str) -> None:
    by_article = {product["article"]: product for product in catalog}
    source = by_article[out_of_stock_sku]
    assert source["quantity"] == 0
    source_category = source["properties"]["KATEGORIYA"]
    assert source_category

    find_analogs = getattr(agent, "find_analogs", None)
    assert callable(find_analogs), (
        "Must have 2 не реализован: нужен app.agent.find_analogs(sku), "
        "возвращающий список товаров с полем reason."
    )
    analogs = find_analogs(out_of_stock_sku)

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
