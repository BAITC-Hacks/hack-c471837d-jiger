"""Must have 1: наличие, остатки по складам и характеристики берутся из каталога.

Проверки идут по данным tests/fixtures/catalog_small.json без обращения к сети
и к модели: инструменты каталога детерминированы.
"""

import json
from pathlib import Path

import pytest

from app import agent, catalog

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CATALOG_FILE = FIXTURES_DIR / "catalog_small.json"
MAIN_STORE = "Основной склад"


@pytest.fixture(autouse=True)
def small_catalog(monkeypatch: pytest.MonkeyPatch, no_openai: None) -> None:
    monkeypatch.setattr(catalog, "CATALOG_PATH", CATALOG_FILE)


@pytest.fixture
def fixture_products() -> list[dict]:
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def by_article(products: list[dict], article: str) -> dict:
    return next(product for product in products if product["article"] == article)


def test_get_product_reports_stock_stores_and_characteristics(fixture_products: list[dict]) -> None:
    """Ответ на «есть ли в наличии» опирается на quantity и склады с остатком."""
    expected = by_article(fixture_products, "TEST-BOX-60-LOW")
    positive = [store for store in expected["stores"] if store["quantity"] > 0]
    assert positive and len(positive) < len(expected["stores"]), (
        "Фикстура должна содержать и склады с остатком, и склады с нулём."
    )

    card = agent.get_product(expected["id"])

    assert card["quantity"] == expected["quantity"] > 0
    assert card["in_stock"] is True
    assert card["price"] == expected["price"]
    assert card["stores"] == [{"name": MAIN_STORE, "quantity": expected["quantity"]}], (
        "В карточку попадают только склады с положительным остатком."
    )
    assert sum(store["quantity"] for store in card["stores"]) == card["quantity"]
    assert card["stores_note"], "Модель должна знать, что остальные склады пусты."
    assert card["description"] == expected["description"]
    assert card["properties"], "Характеристики товара обязательны для Must have 1."
    assert card["min_batch"] == expected["properties"]["KRATNOST_MIN"]


def test_zero_stock_product_has_no_stores(fixture_products: list[dict]) -> None:
    expected = by_article(fixture_products, "TEST-LED-30-ZERO")
    assert expected["quantity"] == 0

    card = agent.get_product(expected["id"])

    assert card["quantity"] == 0 and card["in_stock"] is False
    assert card["stores"] == [], "Нулевой остаток не должен выглядеть как наличие на складе."


def test_search_results_carry_stock_and_stores(fixture_products: list[dict]) -> None:
    expected = by_article(fixture_products, "TEST-BOX-60-LOW")

    found = catalog.search_products(expected["article"], 5)

    assert found["items"], "Существующий артикул должен находиться."
    product = found["items"][0]
    assert product["article"] == expected["article"]
    assert product["quantity"] == expected["quantity"]
    assert product["in_stock"] is True
    assert product["stores"] == [{"name": MAIN_STORE, "quantity": expected["quantity"]}]


def test_in_stock_only_filter_drops_zero_stock(fixture_products: list[dict]) -> None:
    in_stock = sum(1 for product in fixture_products if product["quantity"] > 0)
    assert 0 < in_stock < len(fixture_products)

    filtered = catalog.search_products("", 20, in_stock_only=True)
    everything = catalog.search_products("", 20, min_price=0)

    assert filtered["total"] == in_stock
    assert everything["total"] == len(fixture_products)
    assert all(item["quantity"] > 0 and item["stores"] for item in filtered["items"])


def test_missing_catalog_asks_to_run_the_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "CATALOG_PATH", tmp_path / "products.json")

    with pytest.raises(RuntimeError) as error:
        catalog.load_catalog()

    assert "fetch_catalog.py" in str(error.value), "Ошибка должна подсказывать, что делать."


def test_corrupted_catalog_is_reported_not_treated_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "products.json"
    broken.write_text("{не json", encoding="utf-8")
    monkeypatch.setattr(catalog, "CATALOG_PATH", broken)

    with pytest.raises(RuntimeError) as error:
        catalog.load_catalog()

    assert "повреждён" in str(error.value).casefold()
