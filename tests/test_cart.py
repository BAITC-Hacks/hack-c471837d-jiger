"""Корзина: явное подтверждение, ограничение остатком, изоляция сессий по cookie.

Модель OpenAI подменена сценарием (FakeOpenAI): тест задаёт, какие инструменты
«вызывает» модель на каждом раунде, а проверки идут по серверному состоянию
корзины. Каталог — tests/fixtures/catalog_small.json.
"""

import json
from pathlib import Path

import httpx
import pytest
from openai.types.chat import ChatCompletion

from app import agent, cart, catalog
from app.main import app

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
LOW_STOCK_ID = 900015  # TEST-BOX-60-LOW: остаток 3 шт., цена 53
LOW_STOCK_ARTICLE = "TEST-BOX-60-LOW"
LOW_STOCK_URL = "https://example.test/catalog/junction_boxes/TEST-BOX-60-LOW/"
BOX_A_ID = 900005  # TEST-BOX-60-A: остаток 120 шт., цена 53
CART_URL = "http://localhost:8000/cart"


class FakeOpenAI:
    """Подменяет клиент OpenAI: каждый create() отдаёт следующий шаг сценария.

    Шаг — либо строка (финальный текст модели), либо список вызовов
    инструментов [(имя, аргументы), ...] в одном раунде.
    """

    steps: list = []
    requests: list[list[dict]] = []
    calls = 0

    def __init__(self, **kwargs) -> None:
        self.chat = self
        self.completions = self

    def __enter__(self) -> "FakeOpenAI":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def create(self, **kwargs) -> ChatCompletion:
        FakeOpenAI.requests.append(kwargs["messages"])
        assert FakeOpenAI.steps, "Сценарий модели исчерпан, а агент сделал ещё один запрос."
        step = FakeOpenAI.steps.pop(0)
        if isinstance(step, str):
            message = {"role": "assistant", "content": step}
        else:
            tool_calls = []
            for name, arguments in step:
                FakeOpenAI.calls += 1
                tool_calls.append({
                    "id": f"call_{FakeOpenAI.calls}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                })
            message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        return ChatCompletion.model_validate({
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 0,
            "model": "fake",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        })


@pytest.fixture(autouse=True)
def cart_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "CATALOG_PATH", FIXTURES_DIR / "catalog_small.json")
    monkeypatch.setattr(cart, "_carts", {})
    monkeypatch.setattr(agent, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    FakeOpenAI.steps = []
    FakeOpenAI.requests = []


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def chat(client: httpx.AsyncClient, message: str, steps: list) -> httpx.Response:
    FakeOpenAI.steps = list(steps)
    response = await client.post("/chat", json={"message": message, "history": []})
    assert response.status_code == 200, response.text
    assert not FakeOpenAI.steps, "Агент не дошёл до конца сценария."
    return response


async def api_cart(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/cart")
    assert response.status_code == 200
    return response.json()


def tool_results(name: str) -> list[dict]:
    """Результаты инструмента name за последний ход в порядке вызовов, как их получила модель."""
    messages = FakeOpenAI.requests[-1]
    names = {}
    for entry in messages:
        if entry.get("role") == "assistant":
            for call in entry.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
    return [
        json.loads(entry["content"])
        for entry in messages
        if entry.get("role") == "tool" and names.get(entry["tool_call_id"]) == name
    ]


def cart_rows(data: dict) -> list[tuple]:
    return [(row["article"], row["qty"], row["total"]) for row in data["items"]]


PROPOSE_THREE = [
    [("propose_cart_add", {"product_id": LOW_STOCK_ID, "quantity": 3})],
    "Могу добавить `TEST-BOX-60-LOW` — 3 шт. по 53 ₸, итого 159 ₸. Подтвердите добавление?",
]
CONFIRM = [[("confirm_cart_add", {})], f"Добавил 3 шт. Корзина: {CART_URL}"]


async def test_first_chat_issues_httponly_session_cookie(client: httpx.AsyncClient) -> None:
    response = await chat(client, "Здравствуйте", ["Здравствуйте! Какой товар вы ищете?"])

    header = response.headers["set-cookie"]
    assert header.startswith(f"{cart.SESSION_COOKIE}=")
    lowered = header.casefold()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/" in lowered
    session_id = client.cookies[cart.SESSION_COOKIE]
    assert len(session_id) == 43 and session_id not in response.text

    response = await chat(client, "Спасибо", ["Пожалуйста!"])
    assert "set-cookie" not in response.headers, "Повторный запрос не должен менять сессию."
    assert client.cookies[cart.SESSION_COOKIE] == session_id


async def test_cart_stays_empty_without_explicit_yes(client: httpx.AsyncClient) -> None:
    await chat(client, f"Добавь 3 шт. {LOW_STOCK_ARTICLE}", PROPOSE_THREE)

    [proposal] = tool_results("propose_cart_add")
    assert proposal["status"] == "pending"
    assert (proposal["article"], proposal["quantity"], proposal["total"]) == (LOW_STOCK_ARTICLE, 3, 159)
    assert (await api_cart(client))["items"] == []

    # Модель вызывает confirm_cart_add, хотя покупатель задал вопрос, а не согласился.
    await chat(client, "А какая у неё степень защиты?", [
        [("confirm_cart_add", {})],
        "IP20. Подтвердите добавление?",
    ])
    [refused] = tool_results("confirm_cart_add")
    assert "error" in refused and "подтвержд" in refused["error"].casefold()
    assert (await api_cart(client))["items"] == []


async def test_refusal_and_cancel_do_not_change_cart(client: httpx.AsyncClient) -> None:
    await chat(client, f"Добавь 3 шт. {LOW_STOCK_ARTICLE}", PROPOSE_THREE)

    await chat(client, "Нет, не добавляй", [
        [("confirm_cart_add", {})],
        [("cancel_cart_proposal", {})],
        "Хорошо, не добавляю.",
    ])

    [refused] = tool_results("confirm_cart_add")
    assert "error" in refused
    [cancelled] = tool_results("cancel_cart_proposal")
    assert cancelled == {"status": "cancelled"}
    assert (await api_cart(client))["items"] == []


async def test_four_rejected_three_confirmed_and_cart_matches(client: httpx.AsyncClient) -> None:
    await chat(client, f"Добавь 4 шт. {LOW_STOCK_ARTICLE}", [
        [("propose_cart_add", {"product_id": LOW_STOCK_ID, "quantity": 4})],
        [("propose_cart_add", {"product_id": LOW_STOCK_ID, "quantity": 3})],
        "В наличии только 3 шт. Могу добавить 3 шт. на 159 ₸. Подтвердите добавление?",
    ])

    rejected, accepted = tool_results("propose_cart_add")
    assert "error" in rejected and rejected["max_quantity"] == 3
    assert accepted["status"] == "pending" and accepted["quantity"] == 3
    assert (await api_cart(client))["items"] == [], "До «да» корзина остаётся пустой."

    await chat(client, "да", CONFIRM)

    [confirmed] = tool_results("confirm_cart_add")
    assert confirmed["status"] == "added"
    assert (confirmed["added"], confirmed["qty_in_cart"], confirmed["cart_url"]) == (3, 3, CART_URL)

    data = await api_cart(client)
    assert cart_rows(data) == [(LOW_STOCK_ARTICLE, 3, 159)]
    assert (data["count"], data["total_quantity"], data["total"], data["cart_url"]) == (1, 3, 159, CART_URL)
    assert data["items"][0]["url"] == LOW_STOCK_URL

    page = await client.get("/cart")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert LOW_STOCK_ARTICLE in page.text and "3 шт." in page.text and "159 ₸" in page.text
    assert f'href="{LOW_STOCK_URL}"' in page.text


async def test_stock_limit_counts_items_already_in_cart(client: httpx.AsyncClient) -> None:
    await chat(client, f"Добавь 3 шт. {LOW_STOCK_ARTICLE}", PROPOSE_THREE)
    await chat(client, "да", CONFIRM)

    await chat(client, "Добавь ещё одну", [
        [("propose_cart_add", {"product_id": LOW_STOCK_ID, "quantity": 1})],
        "Весь остаток уже в корзине.",
    ])

    [rejected] = tool_results("propose_cart_add")
    assert "error" in rejected and rejected["max_quantity"] == 0
    assert cart_rows(await api_cart(client)) == [(LOW_STOCK_ARTICLE, 3, 159)]


async def test_other_client_cannot_read_or_change_cart(client: httpx.AsyncClient) -> None:
    await chat(client, f"Добавь 3 шт. {LOW_STOCK_ARTICLE}", PROPOSE_THREE)
    await chat(client, "да", CONFIRM)
    owner_rows = cart_rows(await api_cart(client))
    assert owner_rows == [(LOW_STOCK_ARTICLE, 3, 159)]

    async with new_client() as other:
        # Без cookie и с подделанной cookie чужая корзина не видна.
        assert (await api_cart(other))["items"] == []
        forged = await other.get("/api/cart", headers={"Cookie": f"{cart.SESSION_COOKIE}={'A' * 43}"})
        assert forged.json()["items"] == []
        page = await other.get("/cart")
        assert LOW_STOCK_ARTICLE not in page.text and "Корзина пуста" in page.text

        # Второй клиент получает свою сессию и не может подтвердить чужое предложение.
        response = await chat(other, "да", [[("confirm_cart_add", {})], "Нечего подтверждать."])
        assert response.headers["set-cookie"].startswith(f"{cart.SESSION_COOKIE}=")
        assert other.cookies[cart.SESSION_COOKIE] != client.cookies[cart.SESSION_COOKIE]
        [refused] = tool_results("confirm_cart_add")
        assert "error" in refused
        assert (await api_cart(other))["items"] == []

        # Свои добавления второго клиента не попадают в корзину первого.
        await chat(other, "Добавь 2 шт. TEST-BOX-60-A", [
            [("propose_cart_add", {"product_id": BOX_A_ID, "quantity": 2})],
            "Подтвердите добавление?",
        ])
        await chat(other, "ок", [[("confirm_cart_add", {})], f"Добавил. Корзина: {CART_URL}"])
        assert cart_rows(await api_cart(other)) == [("TEST-BOX-60-A", 2, 106)]

    assert cart_rows(await api_cart(client)) == owner_rows


async def test_forged_cookie_is_replaced_by_server_session(client: httpx.AsyncClient) -> None:
    FakeOpenAI.steps = ["Здравствуйте!"]
    forged = "B" * 43

    response = await client.post(
        "/chat",
        json={"message": "Здравствуйте", "history": []},
        headers={"Cookie": f"{cart.SESSION_COOKIE}={forged}"},
    )

    assert response.status_code == 200
    assert response.headers["set-cookie"].startswith(f"{cart.SESSION_COOKIE}=")
    issued = client.cookies[cart.SESSION_COOKIE]
    assert issued != forged and cart.is_known_session(issued)
    assert not cart.is_known_session(forged)


def test_confirmation_in_same_message_is_rejected() -> None:
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    arguments = json.dumps({"product_id": LOW_STOCK_ID, "quantity": 3})
    message = "да, добавь 3 шт."

    proposal = agent.execute_tool("propose_cart_add", arguments, session_id, message)
    assert proposal["status"] == "pending"
    refused = agent.execute_tool("confirm_cart_add", "{}", session_id, message)
    assert "error" in refused
    assert cart.snapshot(session_id)["items"] == []

    # В следующем сообщении то же «да» уже подтверждает предложение.
    cart.begin_turn(session_id)
    added = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")
    assert added["status"] == "added" and added["cart_url"] == CART_URL
    assert cart_rows(cart.snapshot(session_id)) == [(LOW_STOCK_ARTICLE, 3, 159)]


def test_stale_proposal_cannot_be_confirmed_later() -> None:
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": LOW_STOCK_ID, "quantity": 3}), session_id, ""
    )
    cart.begin_turn(session_id)  # покупатель спросил о другом
    cart.begin_turn(session_id)  # и только потом сказал «да»

    refused = agent.execute_tool("confirm_cart_add", "{}", session_id, "да")

    assert "error" in refused and "устарело" in refused["error"].casefold()
    assert cart.snapshot(session_id)["items"] == []
    assert cart.get_pending(session_id) is None


def test_undelivered_reply_drops_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = cart.new_session()
    monkeypatch.setattr(agent, "_run_agent", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    cart.begin_turn(session_id)
    cart.set_pending(session_id, {
        "product_id": LOW_STOCK_ID, "article": LOW_STOCK_ARTICLE, "name": "Коробка",
        "price": 53, "url": LOW_STOCK_URL, "quantity": 3, "total": 159,
    })
    # Предложение появилось в ходе, ответ которого до покупателя не дошёл: оно снимается.
    cart.end_turn(session_id, completed=False)

    assert cart.get_pending(session_id) is None


@pytest.mark.parametrize("quantity, expected", [
    (5, {"error": True, "min_batch": 10, "suggested_quantity": 10}),
    (15, {"error": True, "min_batch": 10, "suggested_quantity": 10}),
    (30, {"error": True, "max_quantity": 20}),
    (20, {"status": "pending", "quantity": 20, "total": 2000}),
])
def test_min_batch_and_detail_stock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quantity: int, expected: dict,
) -> None:
    # Форма реального каталога: остаток в detail.quantity, кратность в detail.properties.
    product = {
        "id": 1, "name": "Клемма", "article": "T-10", "price": 100,
        "url": "https://example.test/t10/",
        "detail": {"quantity": 25, "properties": {"KRATNOST_MIN": "10"}},
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps([product]), encoding="utf-8")
    monkeypatch.setattr(catalog, "CATALOG_PATH", path)
    session_id = cart.new_session()
    cart.begin_turn(session_id)

    result = agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": 1, "quantity": quantity}), session_id, ""
    )

    for key, value in expected.items():
        if key == "error":
            assert "error" in result
        else:
            assert result[key] == value


def test_invalid_arguments_and_missing_session() -> None:
    session_id = cart.new_session()
    cart.begin_turn(session_id)

    zero = agent.execute_tool("propose_cart_add", json.dumps({"product_id": LOW_STOCK_ID, "quantity": 0}), session_id, "")
    assert "error" in zero
    unknown = agent.execute_tool("propose_cart_add", json.dumps({"product_id": 1, "quantity": 1}), session_id, "")
    assert "не найден" in unknown["error"]
    no_session = agent.execute_tool("propose_cart_add", json.dumps({"product_id": LOW_STOCK_ID, "quantity": 1}), None, "")
    assert "error" in no_session
    extra = agent.execute_tool("confirm_cart_add", json.dumps({"force": True}), session_id, "да")
    assert "error" in extra
    assert cart.snapshot(session_id)["items"] == [] and cart.get_pending(session_id) is None


@pytest.mark.parametrize("message, expected", [
    ("да", True),
    ("Да, добавьте", True),
    ("ок", True),
    ("подтверждаю", True),
    ("добавляй", True),
    ("иә", True),
    ("жарайды", True),
    ("қос", True),
    ("да, 3 шт", True),
    ("нет", False),
    ("не надо", False),
    ("нет, не добавляй", False),
    ("да, но 2 штуки", False),
    ("жоқ", False),
    ("окно 60x40", False),
    ("а сколько стоит?", False),
    ("", False),
    (None, False),
])
def test_is_confirmation(message: object, expected: bool) -> None:
    assert cart.is_confirmation(message) is expected


def test_confirmation_with_other_quantity_is_rejected() -> None:
    session_id = cart.new_session()
    cart.begin_turn(session_id)
    agent.execute_tool(
        "propose_cart_add", json.dumps({"product_id": LOW_STOCK_ID, "quantity": 3}), session_id, ""
    )
    cart.begin_turn(session_id)

    refused = agent.execute_tool("confirm_cart_add", "{}", session_id, "да, 2 шт")

    assert "error" in refused and "количество" in refused["error"].casefold()
    assert cart.snapshot(session_id)["items"] == []
    assert cart.get_pending(session_id) is not None, "Предложение остаётся: модель может переспросить."
