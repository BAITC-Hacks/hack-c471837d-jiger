"""Ограничения ТЗ: приватность логов, кэш каталога, свежий остаток, CORS, условия покупки.

Модель не вызывается (фикстура no_openai), сеть к ekt.kz подменена respx.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from app import agent, cart, catalog, main, stock, terms
from app.prompts import SYSTEM_PROMPT

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
LOW_STOCK_ID = 900015  # TEST-BOX-60-LOW: остаток 3 шт.
DETAIL_URL = "https://ekt.kz/api/products/detail?id=1"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, no_openai: None) -> None:
    monkeypatch.setattr(catalog, "CATALOG_PATH", FIXTURES_DIR / "catalog_small.json")
    monkeypatch.setattr(cart, "_carts", {})
    # .env мог положить учётные данные в окружение: без них API не вызывается.
    monkeypatch.setenv("EKT_API_USER", "")
    monkeypatch.setenv("EKT_API_PASSWORD", "")
    monkeypatch.delenv("LOG_LEVEL", raising=False)


def write_catalog(path: Path, products: list[dict]) -> None:
    path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")


def live_product(quantity: int = 5, link: str = DETAIL_URL) -> dict:
    """Товар в форме реального кэша: остаток в detail.quantity, ссылка API в url_api_detail."""
    return {
        "id": 1, "name": "Клемма", "article": "T-1", "price": 100,
        "url": "https://ekt.kz/catalog/t1/", "url_api_detail": link,
        "detail": {"quantity": quantity, "stores": [{"name": "Алматы", "quantity": quantity}]},
    }


def session_with_pending(product_id: int, quantity: int) -> str:
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    proposal = agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": product_id, "quantity": quantity}), session_id, ""
    )
    assert proposal["status"] == "pending", proposal
    cart.begin_turn(session_id)
    return session_id


# ---------- Приватность: текст покупателя не попадает в лог по умолчанию ----------

def test_default_log_hides_message_text_and_tool_arguments(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")  # агент останавливается до вызова модели
    secret_text = "Мой номер карты 4111 1111 1111 1111"

    agent.run_agent(secret_text, [])
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": LOW_STOCK_ID, "quantity": 1}), session_id, secret_text
    )
    agent.finish("Ответ с деталями заказа")

    output = capsys.readouterr().out
    assert "4111" not in output and "номер карты" not in output
    assert "Запрос: 35 символов, история: 0 реплик" in output
    assert "Инструмент: propose_cart_add\n" in output
    assert "деталями заказа" not in output and "Ответ: 23 символов" in output


def test_debug_log_level_writes_full_content(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    agent.run_agent("Есть ли автомат 25 А?", [])
    agent.finish("Полный текст ответа")

    output = capsys.readouterr().out
    assert "Запрос: Есть ли автомат 25 А?" in output
    assert "Ответ: Полный текст ответа" in output


def test_prompt_forbids_payment_data_and_requires_escalation_contacts() -> None:
    assert "CVV" in SYSTEM_PROMPT and "PIN" in SYSTEM_PROMPT and "SMS" in SYSTEM_PROMPT
    assert "Эскалация на менеджера" in SYSTEM_PROMPT
    assert "Условия покупки и контакты магазина" in SYSTEM_PROMPT


# ---------- Скорость: каталог в памяти, индекс по артикулу ----------

def test_catalog_is_read_once_and_reloaded_only_when_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "products.json"
    write_catalog(path, [live_product(quantity=5)])
    monkeypatch.setattr(catalog, "CATALOG_PATH", path)

    first = catalog.load_catalog()
    assert catalog.load_catalog() is first, "Повторный вызов не должен перечитывать диск."
    assert catalog.find_product(1) is first[0]

    write_catalog(path, [live_product(quantity=2)])
    changed = catalog.load_catalog()
    assert changed is not first and changed[0]["detail"]["quantity"] == 2

    forced = catalog.reload_catalog()
    assert forced is not changed and forced[0]["detail"]["quantity"] == 2
    assert catalog.load_catalog() is forced


def test_find_by_article_uses_index_including_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product = live_product()
    product["detail"]["properties"] = {"CML2_ARTICLE": "SUP-001_", "ARTIKULPOSTAVSHCHIKA": "sup.0002"}
    path = tmp_path / "products.json"
    write_catalog(path, [product, {"id": 2, "name": "Другой", "article": "T-2", "price": 1}])
    monkeypatch.setattr(catalog, "CATALOG_PATH", path)

    assert catalog.find_by_article("  t-1 ")["id"] == 1
    assert catalog.find_by_article("sup-001_")["id"] == 1
    assert catalog.find_by_article("SUP.0002")["id"] == 1
    assert catalog.find_by_article("T-2")["id"] == 2
    assert catalog.find_by_article("T-1-MISSING") is None
    assert catalog.find_by_article(None) is None
    assert catalog.find_product("2")["article"] == "T-2" and catalog.find_product(3) is None


def test_unknown_product_id_gets_hint_from_article_in_buyer_message() -> None:
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    arguments = json.dumps({"product_id": 6015, "quantity": 2})

    hinted = agent.execute_tool(
        "propose_cart_add", arguments, session_id, "Добавь 2 шт. test-box-60-low в корзину"
    )
    plain = agent.execute_tool("propose_cart_add", arguments, session_id, "Добавь две коробки")
    ambiguous = agent.execute_tool(
        "propose_cart_add", arguments, session_id, "Добавь TEST-LED-30-A и TEST-LED-30-B"
    )

    assert "error" in hinted and hinted["hint"]["id"] == LOW_STOCK_ID
    assert str(LOW_STOCK_ID) in hinted["error"]
    assert "error" in plain and "hint" not in plain
    assert "error" in ambiguous and "hint" not in ambiguous
    assert cart.get_pending(session_id) is None, "Подсказка ничего не предлагает и не добавляет."


def test_exact_article_search_still_ranks_first_after_indexing() -> None:
    found = catalog.search_products("Есть TEST-BOX-60-LOW 7 шт?", 5)

    assert found["items"] and found["items"][0]["id"] == LOW_STOCK_ID
    assert catalog.search_products("TEST-BOX-60-LOW-MISSING", 5)["items"] == []


# ---------- Актуальность: свежий остаток перед подтверждением ----------

@pytest.fixture
def live_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "products.json"
    write_catalog(path, [live_product(quantity=5)])
    monkeypatch.setattr(catalog, "CATALOG_PATH", path)
    monkeypatch.setenv("EKT_API_USER", "user")
    monkeypatch.setenv("EKT_API_PASSWORD", "password")
    return path


@respx.mock
def test_confirmation_uses_fresh_quantity_from_api(live_catalog: Path) -> None:
    route = respx.get(DETAIL_URL).respond(
        200, json={"id": 1, "quantity": 1, "stores": [{"name": "Алматы", "quantity": 1}]}
    )
    session_id = session_with_pending(product_id=1, quantity=3)

    refused = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert route.called and route.calls.last.request.headers.get("authorization", "").startswith("Basic ")
    assert "error" in refused and refused["max_quantity"] == 1
    assert cart.snapshot(session_id)["items"] == []
    # Кэш в памяти получил свежий остаток: повторное предложение уже не превысит его.
    assert catalog.find_product(1)["detail"]["quantity"] == 1
    cart.begin_turn(session_id)
    again = agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": 1, "quantity": 3}), session_id, ""
    )
    assert again["status"] == "pending" and again["quantity"] == 1
    assert again["max_quantity"] == 1 and again.get("adjusted")


@respx.mock
def test_confirmation_with_fresh_quantity_adds_and_reports_live_source(live_catalog: Path) -> None:
    respx.get(DETAIL_URL).respond(200, json={"id": 1, "quantity": 4, "stores": []})
    session_id = session_with_pending(product_id=1, quantity=3)

    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert added["status"] == "added" and added["stock_source"] == "live"
    assert "stock_note" not in added


@respx.mock
def test_api_failure_falls_back_to_cache_and_tells_the_buyer(live_catalog: Path) -> None:
    respx.get(DETAIL_URL).mock(side_effect=httpx.ConnectError("нет сети"))
    session_id = session_with_pending(product_id=1, quantity=3)

    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert added["status"] == "added" and added["stock_source"] == "cache"
    assert "кэш" in added["stock_note"].casefold()
    assert catalog.find_product(1)["detail"]["quantity"] == 5, "Кэш при ошибке сети не меняется."


@respx.mock
def test_slow_api_is_abandoned_after_timeout(live_catalog: Path) -> None:
    respx.get(DETAIL_URL).mock(side_effect=httpx.ReadTimeout("медленно"))
    session_id = session_with_pending(product_id=1, quantity=3)

    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert added["status"] == "added" and added["stock_source"] == "cache" and added["stock_note"]
    assert stock.LIVE_TIMEOUT == 5.0


@respx.mock
def test_without_credentials_api_is_not_called(live_catalog: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKT_API_USER", "")
    route = respx.get(DETAIL_URL).respond(200, json={"id": 1, "quantity": 0})
    session_id = session_with_pending(product_id=1, quantity=3)

    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert not route.called
    assert added["status"] == "added" and added["stock_source"] == "cache"
    assert "stock_note" not in added, "Без учётных данных запроса не было — и предупреждать не о чем."


@pytest.mark.parametrize("link", [
    "https://evil.test/api/products/detail?id=1",
    "http://ekt.kz/api/products/detail?id=1",
    "https://ekt.kz:8443/api/products/detail?id=1",
    "",
    None,
])
def test_basic_auth_goes_only_to_ekt_over_https(link: object) -> None:
    assert stock.detail_url(live_product(link=link)) is None


@respx.mock
def test_mismatched_or_invalid_api_payload_is_ignored(live_catalog: Path) -> None:
    respx.get(DETAIL_URL).respond(200, json={"id": 999, "quantity": 0})
    session_id = session_with_pending(product_id=1, quantity=3)

    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert added["status"] == "added" and added["stock_source"] == "cache" and added["stock_note"]


# ---------- CORS из окружения ----------

@pytest.mark.parametrize("raw, expected", [
    ("", ["*"]),
    ("*", ["*"]),
    ("https://ekt.kz", ["https://ekt.kz"]),
    (" https://ekt.kz/ , https://www.ekt.kz ", ["https://ekt.kz", "https://www.ekt.kz"]),
])
def test_allowed_origins_are_parsed_from_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", raw)

    assert main.allowed_origins() == expected


async def test_default_cors_allows_any_origin_without_credentials(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/cart", headers={"Origin": "https://example.test"})

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in response.headers


# ---------- Условия покупки и контакты для эскалации ----------

def test_terms_block_reports_missing_file_and_follows_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "terms.md"
    monkeypatch.setattr(terms, "TERMS_PATH", path)
    monkeypatch.setattr(terms, "_cache", {"key": None, "text": ""})

    assert "не заданы" in terms.terms_note()

    path.write_text("## Контакты\n- Телефон: +7 (727) 000-00-00", encoding="utf-8")
    assert "+7 (727) 000-00-00" in terms.terms_note()

    path.write_text("## Контакты\n- Телефон: +7 (727) 111-11-11", encoding="utf-8")
    assert "111-11-11" in terms.terms_note() and "000-00-00" not in terms.terms_note()


def test_shipped_terms_file_has_payment_delivery_and_manager_contacts() -> None:
    text = (ROOT / "data" / "terms.md").read_text(encoding="utf-8")

    assert "## Оплата" in text and "## Доставка" in text and "## Контакты" in text
    assert "+7 (727) 346-88-88" in text and "@ekt.kz" in text
    assert len(text) <= terms.MAX_TERMS_CHARS, "Весь файл должен помещаться в промпт."
