"""Offline product-tool checks using real catalog loading and mocked EKT HTTP.

Install test dependencies: python -m pip install pytest pytest-asyncio respx
Run: python -m pytest tests/test_tools_product.py

search_products returns a list: [] is its "not found" result. get_product
returns an error message for an unknown ID. No model-generated text is tested.
"""

import re

import pytest

from app import agent

pytestmark = pytest.mark.usefixtures("offline_catalog")

CERTIFICATE_URL = re.compile(r"https?://[^\s<>\"']+\.pdf(?:\?[^\s<>\"']*)?")


def test_search_by_existing_sku(catalog: list[dict], in_stock_sku: str) -> None:
    """Must have 1: an exact article exposes availability and product characteristics."""
    expected = next(product for product in catalog if product["article"] == in_stock_sku)

    results = agent.search_products(in_stock_sku)

    assert results, "Существующий артикул не найден."
    product = results[0]
    assert product["id"] == expected["id"], "Точное совпадение артикула должно быть первым."
    assert product["article"] == in_stock_sku
    assert product["name"] == expected["name"]
    assert product["price"] == expected["price"]
    assert product["quantity"] == expected["quantity"] > 0
    assert product["properties"] == expected["properties"]
    assert product["description"] == expected["description"]


@pytest.mark.parametrize("certificate_index", [0, 1], ids=["certificate-1", "certificate-2"])
def test_product_with_certificate_returns_link(catalog: list[dict], certificate_index: int) -> None:
    certified = [product for product in catalog if CERTIFICATE_URL.search(product["description"])]
    assert len(certified) >= 2, "В фикстуре должны быть два товара с сертификатами."
    expected = certified[certificate_index]
    expected_links = set(CERTIFICATE_URL.findall(expected["description"]))

    product = agent.get_product(expected["id"])

    assert product["id"] == expected["id"]
    assert product["article"] == expected["article"]
    # get_product exposes the unchanged detail response under the detail key.
    actual_links = set(CERTIFICATE_URL.findall(product["detail"]["description"]))
    assert actual_links == expected_links, "Ссылка сертификата потеряна или подменена."


def test_unknown_sku(catalog: list[dict], in_stock_sku: str) -> None:
    articles = {product["article"] for product in catalog}
    missing_sku = f"{in_stock_sku}-MISSING"
    while missing_sku in articles:
        missing_sku += "-MISSING"

    results = agent.search_products(missing_sku)

    assert results == [], (
        "Неизвестный артикул должен давать «не найдено» (пустой список), "
        "а не товары с частично совпадающим артикулом."
    )


@pytest.mark.parametrize("suffix", ["-MISSING", "_MISSING", "/MISSING", ".MISSING"])
def test_unknown_sku_does_not_fall_back_to_matching_name(
    catalog: list[dict], in_stock_sku: str, suffix: str,
) -> None:
    source = next(product for product in catalog if product["article"] == in_stock_sku)
    missing_sku = in_stock_sku + suffix
    assert missing_sku not in {product["article"] for product in catalog}

    assert agent.search_products(f"{source['name'].split()[0]} {missing_sku}") == []


def test_article_search_ignores_case_and_outer_whitespace(in_stock_sku: str) -> None:
    results = agent.search_products(f"  {in_stock_sku.lower()}  ")

    assert results and results[0]["article"] == in_stock_sku


def test_known_article_in_a_question_with_requested_quantity(in_stock_sku: str) -> None:
    results = agent.search_products(f"Есть {in_stock_sku} 7 шт?")

    assert results and results[0]["article"] == in_stock_sku


@pytest.mark.parametrize("power", ["30 Вт", "30Вт"])
def test_search_by_category_and_characteristics(catalog: list[dict], power: str) -> None:
    expected_ids = {
        product["id"] for product in catalog
        if product["properties"]["KATEGORIYA"] == "Трековый светильник"
    }

    results = agent.search_products(f"трековый светильник {power} IP20")

    assert results
    assert {product["id"] for product in results}.issubset(expected_ids)
    assert all("30 Вт" in product["description"] and "IP20" in product["description"] for product in results)


def test_specification_is_not_matched_as_a_prefix() -> None:
    assert agent.search_products("трековый IP2") == []


class TestArticleAliases:
    @pytest.fixture
    def catalog(self, catalog: list[dict]) -> list[dict]:
        # Change only the source data; offline_catalog still loads it through mocked EKT HTTP.
        catalog[0]["article"] = "000123_"
        catalog[0]["properties"]["CML2_ARTICLE"] = "SUP-001_"
        catalog[0]["properties"]["ARTIKULPOSTAVSHCHIKA"] = "SUP.0002"
        return catalog

    @pytest.mark.parametrize("query", ["000123_", "sup-001_", "SUP.0002"])
    def test_search_by_article_alias(self, catalog: list[dict], query: str) -> None:
        results = agent.search_products(query)

        assert results and results[0]["id"] == catalog[0]["id"]
        assert results[0]["article"] == "000123_"

    def test_article_leading_zeroes_are_significant(self) -> None:
        assert agent.search_products("123_") == []


class TestCompactMeasurements:
    @pytest.fixture
    def catalog(self, catalog: list[dict]) -> list[dict]:
        for product in catalog:
            product["name"] = product["name"].replace("30 мА", "30мА")
            product["description"] = product["description"].replace("30 мА", "30мА")
        return catalog

    def test_spaced_unit_finds_compact_catalog_measurement(self, catalog: list[dict]) -> None:
        expected_ids = {
            product["id"] for product in catalog
            if product["properties"]["KATEGORIYA"] == "Дифференциальный автомат"
        }

        results = agent.search_products("дифференциальный автомат 30 мА")

        assert results
        assert {product["id"] for product in results}.issubset(expected_ids)


def test_unknown_product_id_returns_not_found(catalog: list[dict]) -> None:
    missing_id = max(product["id"] for product in catalog) + 1

    product = agent.get_product(missing_id)

    assert set(product) == {"error"}, "Для отсутствующего ID нельзя возвращать карточку товара."
    assert "не найден" in product["error"].casefold()
