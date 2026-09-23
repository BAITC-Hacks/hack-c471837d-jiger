"""Offline product-tool checks using real catalog loading and mocked EKT HTTP.

Install test dependencies: python -m pip install pytest pytest-asyncio respx
Run: python -m pytest tests/test_tools_product.py

search_products returns a list: [] is its "not found" result. get_product
returns an error message for an unknown ID. No model-generated text is tested.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

import fetch_catalog as catalog_loader
from app import agent
from app import catalog as product_catalog

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CERTIFICATE_URL = re.compile(r"https?://[^\s<>\"']+\.pdf(?:\?[^\s<>\"']*)?")


@pytest.fixture(autouse=True)
def offline_product_catalog(
    catalog: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Build the production cache format without replacing the tools or their results."""
    def forbid_openai(*args, **kwargs):
        pytest.fail("Инструменты каталога не должны создавать клиент OpenAI.")

    monkeypatch.setattr(agent, "OpenAI", forbid_openai)
    cache_path = tmp_path / "data" / "products.json"
    monkeypatch.setattr(product_catalog, "CATALOG_PATH", cache_path)
    monkeypatch.setattr(catalog_loader, "CATALOG_PATH", cache_path)

    # Keep the captured list-response schema; catalog_small contains detail objects.
    page = json.loads((FIXTURES_DIR / "api_products_page1.json").read_text(encoding="utf-8"))
    list_fields = tuple(page["items"][0])
    page["items"] = [
        {
            field: (
                f"{catalog_loader.API_URL}/detail?id={product['id']}"
                if field == "url_api_detail" else product[field]
            )
            for field in list_fields
        }
        for product in catalog
    ]
    page["page"] = 1
    page["count"] = len(page["items"])
    assert page["count"] <= page["per_page"], "Тестовый каталог должен помещаться на одну страницу."

    # Unknown HTTP requests fail, including accidental requests to OpenAI or PDFs.
    # Every registered EKT route must be consumed by the real catalog loader.
    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        router.get(catalog_loader.API_URL, params={"page": "1"}).respond(200, json=page)
        for product in catalog:
            router.get(
                f"{catalog_loader.API_URL}/detail", params={"id": str(product["id"])},
            ).respond(200, json=product)

        with httpx.Client(auth=httpx.BasicAuth("fixture-user", "fixture-password")) as client:
            products, pages = catalog_loader.fetch_catalog(client, max_pages=1)
        assert pages == 1 and len(products) == len(catalog)
        catalog_loader.save_catalog(products)
        yield


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


def test_unknown_product_id_returns_not_found(catalog: list[dict]) -> None:
    missing_id = max(product["id"] for product in catalog) + 1

    product = agent.get_product(missing_id)

    assert set(product) == {"error"}, "Для отсутствующего ID нельзя возвращать карточку товара."
    assert "не найден" in product["error"].casefold()
