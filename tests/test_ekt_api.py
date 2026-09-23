"""Live API checks; run with pytest -o 'addopts=' -m live tests/test_ekt_api.py.

EKT_USER is required even when EKT_API_USER exists. The password is read from
EKT_PASSWORD, falling back to the project's EKT_API_PASSWORD variable.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv

API_URL = "https://ekt.kz/api/products"
pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def ekt_user() -> str:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    user = os.environ.get("EKT_USER")
    if not user:
        pytest.skip("EKT_USER не задан: live-проверки API пропущены.")
    return user


@pytest.fixture
def ekt_client(ekt_user: str) -> Iterator[httpx.Client]:
    with httpx.Client(timeout=15.0, follow_redirects=False) as client:
        yield client


def test_without_authorization_returns_401(ekt_client: httpx.Client) -> None:
    response = ekt_client.get(API_URL, params={"page": 1}, auth=None)

    assert response.status_code == 401


def test_wrong_password_returns_401(ekt_client: httpx.Client, ekt_user: str) -> None:
    wrong_password = f"invalid-{uuid4().hex}"
    if wrong_password in (os.environ.get("EKT_PASSWORD"), os.environ.get("EKT_API_PASSWORD")):
        wrong_password += "-invalid"
    response = ekt_client.get(
        API_URL,
        params={"page": 1},
        auth=httpx.BasicAuth(ekt_user, wrong_password),
    )

    assert response.status_code == 401


def test_valid_credentials_return_200(ekt_client: httpx.Client, ekt_user: str) -> None:
    password = os.environ.get("EKT_PASSWORD") or os.environ.get("EKT_API_PASSWORD")
    if not password:
        pytest.skip("EKT_PASSWORD или EKT_API_PASSWORD не задан: проверка верных кредов пропущена.")
    response = ekt_client.get(
        API_URL,
        params={"page": 1},
        auth=httpx.BasicAuth(ekt_user, password),
    )

    assert response.status_code == 200


@pytest.fixture
def ekt_auth(ekt_user: str) -> httpx.BasicAuth:
    password = os.environ.get("EKT_PASSWORD") or os.environ.get("EKT_API_PASSWORD")
    if not password:
        pytest.skip("EKT_PASSWORD или EKT_API_PASSWORD не задан: проверка каталога пропущена.")
    return httpx.BasicAuth(ekt_user, password)


@pytest.fixture(scope="module")
def api_page_sample() -> dict:
    path = Path(__file__).resolve().parent / "fixtures" / "api_products_page1.json"
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(sample, dict) and sample["items"], "В образце API должны быть товары."
    return sample


@pytest.fixture(scope="module")
def required_product_fields(api_page_sample: dict) -> set[str]:
    # Read field names from the captured API response, not the normalized cache.
    return set(api_page_sample["items"][0])


def _get_product_page(client: httpx.Client, auth: httpx.BasicAuth, page: int) -> dict:
    response = client.get(API_URL, params={"page": page}, auth=auth)
    assert response.status_code == 200, f"Страница {page}: HTTP {response.status_code}."
    payload = response.json()
    assert isinstance(payload, dict), "Ожидался JSON-объект страницы каталога."
    assert payload.get("page") == page, "API вернул неверный номер страницы."
    assert isinstance(payload.get("items"), list), "Поле items должно быть списком."
    return payload


def _assert_product_fields(items: list, required_fields: set[str]) -> None:
    for index, item in enumerate(items):
        assert isinstance(item, dict), f"Товар с индексом {index} должен быть объектом."
        missing = required_fields - item.keys()
        assert not missing, f"У товара с индексом {index} отсутствуют поля: {sorted(missing)}."
        # A present field may be null: the real sample contains image=null.


def test_first_page_is_not_empty(ekt_client: httpx.Client, ekt_auth: httpx.BasicAuth) -> None:
    page = _get_product_page(ekt_client, ekt_auth, 1)

    assert page["items"], "Первая страница каталога не должна быть пустой."


def test_second_page_differs_from_first(
    ekt_client: httpx.Client, ekt_auth: httpx.BasicAuth,
) -> None:
    first = _get_product_page(ekt_client, ekt_auth, 1)
    second = _get_product_page(ekt_client, ekt_auth, 2)
    assert first["items"], "Первая страница каталога не должна быть пустой."

    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    assert first_ids != second_ids, "Страница 2 повторяет товары страницы 1."


@pytest.mark.parametrize("page_number", [1, 2], ids=["page-1", "page-2"])
def test_products_have_required_fields(
    ekt_client: httpx.Client,
    ekt_auth: httpx.BasicAuth,
    required_product_fields: set[str],
    page_number: int,
) -> None:
    page = _get_product_page(ekt_client, ekt_auth, page_number)

    _assert_product_fields(page["items"], required_product_fields)


def test_very_large_page_returns_valid_response(
    ekt_client: httpx.Client,
    ekt_auth: httpx.BasicAuth,
    api_page_sample: dict,
    required_product_fields: set[str],
) -> None:
    response = ekt_client.get(API_URL, params={"page": 1_000_000_000}, auth=ekt_auth)

    assert response.status_code in (200, 400, 404, 422), (
        "Ожидался успешный ответ либо ошибка номера страницы, "
        f"получен HTTP {response.status_code}."
    )
    payload = response.json()
    if response.status_code != 200:
        assert isinstance(payload, dict), "Ошибка страницы должна быть JSON-объектом."
        assert any(payload.get(key) for key in ("error", "errors", "detail", "message")), (
            "Ответ должен объяснять ошибку номера страницы."
        )
        return

    if isinstance(payload, list):
        assert payload == [], "Вне объекта страницы допустим только пустой список."
        return

    assert isinstance(payload, dict), "Ожидался объект страницы или пустой список."
    assert set(api_page_sample).issubset(payload), "Нарушена структура ответа страницы."
    assert type(payload["page"]) is int and payload["page"] > 0
    assert type(payload["per_page"]) is int and payload["per_page"] > 0
    assert type(payload["count"]) is int and payload["count"] >= 0
    assert isinstance(payload["items"], list), "Поле items должно быть списком."
    assert len(payload["items"]) <= payload["per_page"]
    _assert_product_fields(payload["items"], required_product_fields)
