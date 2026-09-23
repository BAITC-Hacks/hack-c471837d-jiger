import json
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx

import fetch_catalog as catalog_loader
from app import agent
from app import catalog as product_catalog
from app.main import app

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def catalog() -> list[dict]:
    return json.loads((FIXTURES_DIR / "catalog_small.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Офлайн-тесты не выходят в сеть: живой остаток и каталог берутся из моков.

    Без этой защиты app.stock.live_quantity обратился бы к ekt.kz, как только
    в окружении разработчика окажутся EKT_API_USER и EKT_API_PASSWORD.
    Тесты с меткой live обращаются к API намеренно и не ограничиваются.
    """
    if request.node.get_closest_marker("live"):
        return
    original = socket.socket.connect

    def forbid_connect(self, address, *args, **kwargs):
        raise AssertionError(
            f"Офлайн-тест попытался открыть сетевое соединение с {address}. "
            "Замокайте запрос (respx) или пометьте тест как live."
        )

    monkeypatch.setattr(socket.socket, "connect", forbid_connect)
    assert socket.socket.connect is not original


@pytest.fixture
def no_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in guard for deterministic tests, including ones without a catalog."""
    def forbid_openai(*args, **kwargs):
        pytest.fail("Тесты логики не должны создавать клиент OpenAI.")

    monkeypatch.setattr(agent, "OpenAI", forbid_openai)


@pytest.fixture
def offline_catalog(
    catalog: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_openai: None,
) -> Iterator[None]:
    """Build an isolated cache through the real loader and keep HTTP mocked until teardown."""
    cache_path = tmp_path / "data" / "products.json"
    monkeypatch.setattr(product_catalog, "CATALOG_PATH", cache_path)
    monkeypatch.setattr(catalog_loader, "CATALOG_PATH", cache_path)

    # catalog_small contains detail responses; preserve the captured list schema.
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

    with respx.mock(assert_all_mocked=True, assert_all_called=True) as router:
        router.get(catalog_loader.API_URL, params={"page": "1"}).respond(200, json=page)
        for product in catalog:
            router.get(
                f"{catalog_loader.API_URL}/detail", params={"id": str(product["id"])},
            ).respond(200, json=product)

        with httpx.Client(auth=httpx.BasicAuth("fixture-user", "fixture-password")) as client:
            products, pages = catalog_loader.fetch_catalog(client, max_pages=1)
        assert pages == 1 and len(products) == len(catalog)
        # Verify the loader consumed every route before the test can make requests.
        router.assert_all_called()
        catalog_loader.save_catalog(products)
        yield


@pytest.fixture
def terms() -> str:
    return (FIXTURES_DIR / "terms.md").read_text(encoding="utf-8")


@pytest.fixture
def in_stock_sku(catalog: list[dict]) -> str:
    return next(product["article"] for product in catalog if product["quantity"] > 0)


@pytest.fixture
def out_of_stock_sku(catalog: list[dict]) -> str:
    return next(product["article"] for product in catalog if product["quantity"] == 0)


@pytest.fixture
def low_stock_sku(catalog: list[dict]) -> str:
    return next(product["article"] for product in catalog if 0 < product["quantity"] <= 3)


@pytest.fixture
def session_id() -> str:
    return str(uuid4())


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
