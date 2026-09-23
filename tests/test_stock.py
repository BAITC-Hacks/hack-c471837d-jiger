"""Свежий остаток из API ekt.kz перед подтверждением (Must have 4).

Сеть замокана respx: реальных запросов к ekt.kz тесты не делают. Отдельно
проверяется, что Basic Auth уходит только на ekt.kz, а любая ошибка API
не ломает подтверждение, а откатывает проверку на кэш каталога.
"""

import httpx
import pytest
import respx

from app import stock

DETAIL_URL = "https://ekt.kz/api/products/detail?id=42"


@pytest.fixture(autouse=True)
def api_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKT_API_USER", "demo-user")
    monkeypatch.setenv("EKT_API_PASSWORD", "demo-password")


def make_product(**overrides: object) -> dict:
    product = {
        "id": 42,
        "article": "T-42",
        "url_api_detail": DETAIL_URL,
        "detail": {"id": 42, "quantity": 5, "stores": [{"name": "Алматы", "quantity": 5}]},
    }
    product.update(overrides)
    return product


# ---------- Учётные данные ----------

@pytest.mark.parametrize("user, password", [("", "p"), ("u", ""), ("  ", "p")])
def test_incomplete_credentials_disable_the_live_check(
    monkeypatch: pytest.MonkeyPatch, user: str, password: str
) -> None:
    monkeypatch.setenv("EKT_API_USER", user)
    monkeypatch.setenv("EKT_API_PASSWORD", password)

    assert stock.credentials() is None


def test_without_credentials_no_request_is_made(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EKT_API_USER", raising=False)
    monkeypatch.delenv("EKT_API_PASSWORD", raising=False)

    with respx.mock(assert_all_called=False) as router:
        route = router.get(DETAIL_URL).respond(200, json={"id": 42, "quantity": 1})
        assert stock.live_quantity(make_product()) is None

    assert not route.called, "Без учётных данных запрос к API выполняться не должен."


# ---------- Basic Auth уходит только на ekt.kz ----------

@pytest.mark.parametrize("link", [
    "https://evil.test/api/products/detail?id=42",
    "http://ekt.kz/api/products/detail?id=42",
    "https://ekt.kz.evil.test/api/products/detail?id=42",
    "https://ekt.kz:8443/api/products/detail?id=42",
    "ftp://ekt.kz/api/products/detail?id=42",
    "не ссылка",
    "",
    None,
])
def test_credentials_never_leave_the_origin_server(link: object) -> None:
    product = make_product(url_api_detail=link)

    assert stock.detail_url(product) is None

    with respx.mock(assert_all_called=False) as router:
        catch_all = router.route().respond(200, json={"id": 42, "quantity": 1})
        assert stock.live_quantity(product) is None

    assert not catch_all.called, "Запрос на посторонний адрес недопустим."


def test_request_to_ekt_carries_basic_auth() -> None:
    with respx.mock as router:
        route = router.get(DETAIL_URL).respond(200, json={"id": 42, "quantity": 3})

        stock.live_quantity(make_product())

    assert route.called
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


# ---------- Успешный ответ обновляет кэш ----------

def test_live_quantity_refreshes_cached_stock_and_stores() -> None:
    product = make_product()
    fresh_stores = [{"name": "Шымкент", "quantity": 2}]

    with respx.mock as router:
        router.get(DETAIL_URL).respond(200, json={
            "id": 42, "quantity": 2, "stores": fresh_stores,
        })

        assert stock.live_quantity(product) == 2

    assert product["detail"]["quantity"] == 2, "Кэш каталога должен получить свежий остаток."
    assert product["detail"]["stores"] == fresh_stores


def test_negative_quantity_is_clamped_to_zero() -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).respond(200, json={"id": 42, "quantity": -4})

        assert stock.live_quantity(product) == 0

    assert product["detail"]["quantity"] == 0


# ---------- Ошибки API откатывают проверку на кэш ----------

@pytest.mark.parametrize("failure", [
    {"status_code": 500},
    {"status_code": 401},
    {"status_code": 404},
])
def test_http_errors_fall_back_to_the_cache(failure: dict) -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).respond(**failure)

        assert stock.live_quantity(product) is None

    assert product["detail"]["quantity"] == 5, "Остаток из кэша не должен затираться."


def test_network_error_falls_back_to_the_cache() -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).mock(side_effect=httpx.ConnectError("нет сети"))

        assert stock.live_quantity(product) is None

    assert product["detail"]["quantity"] == 5


def test_timeout_falls_back_to_the_cache() -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).mock(side_effect=httpx.ReadTimeout("долго"))

        assert stock.live_quantity(product) is None

    assert product["detail"]["quantity"] == 5


@pytest.mark.parametrize("payload, label", [
    ({"id": 43, "quantity": 1}, "ответ о другом товаре"),
    ({"quantity": 1}, "ответ без id"),
    ({"id": 42}, "ответ без остатка"),
    ({"id": 42, "quantity": None}, "остаток null"),
    ({"id": 42, "quantity": "много"}, "остаток строкой"),
    ({"id": 42, "quantity": True}, "остаток булевым"),
    ([{"id": 42, "quantity": 1}], "список вместо объекта"),
])
def test_unexpected_payload_is_not_trusted(payload: object, label: str) -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).respond(200, json=payload)

        assert stock.live_quantity(product) is None, f"Нельзя доверять: {label}."

    assert product["detail"]["quantity"] == 5


def test_non_json_body_is_not_trusted() -> None:
    product = make_product()

    with respx.mock as router:
        router.get(DETAIL_URL).respond(200, text="<html>техработы</html>")

        assert stock.live_quantity(product) is None

    assert product["detail"]["quantity"] == 5
