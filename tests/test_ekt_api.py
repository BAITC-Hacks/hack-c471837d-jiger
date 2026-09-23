"""Live Basic Auth checks; run with pytest -o 'addopts=' -m live tests/test_ekt_api.py.

EKT_USER is required even when EKT_API_USER exists. The password is read from
EKT_PASSWORD, falling back to the project's EKT_API_PASSWORD variable.
"""

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
