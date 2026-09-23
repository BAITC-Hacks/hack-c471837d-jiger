import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.main import app

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def catalog() -> list[dict]:
    return json.loads((FIXTURES_DIR / "catalog_small.json").read_text(encoding="utf-8"))


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
