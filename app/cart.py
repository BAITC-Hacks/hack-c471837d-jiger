"""Корзина покупателя: in-memory хранилище, привязанное к cookie сессии.

Схема хранения (всё под одним threading.Lock, потому что FastAPI выполняет
синхронные обработчики в пуле потоков):

    session_id -> {
        "items": {product_id: {"qty", "name", "article", "price", "url"}},
        "pending": {"items": список предложений, "turn": номер хода} | None,
        "turn": номер текущего сообщения покупателя,
        "updated": время последнего обращения,
    }

Идентификатор сессии выдаёт сервер (secrets.token_urlsafe) и хранит его только
в HttpOnly cookie; в теле запросов идентификаторов корзины нет. Неизвестный или
подделанный идентификатор даёт пустую корзину и ничего не создаёт.

Товар попадает в items только через commit_pending, а он выполняется лишь
когда текущее сообщение покупателя распознано как явное подтверждение
(is_confirmation) и предложение было создано в предыдущем сообщении.
"""

import copy
import os
import re
import secrets
import threading
import time

SESSION_COOKIE = "ekt_session"
SESSION_TTL = 7 * 24 * 3600  # секунды бездействия, после которых сессия удаляется
MAX_SESSIONS = 10_000
DEFAULT_PUBLIC_BASE_URL = "http://localhost:8000"
MAX_PENDING_ITEMS = 20
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Явное согласие: отдельные слова, а не части других слов («окно», «дачный»).
CONFIRM_RE = re.compile(
    r"(?<![\w-])(?:"
    r"да|ага|угу|ок|окей|подтверждаю|подтверждаем|согласен|согласна"
    r"|добавь|добавьте|добавляй|добавляйте"
    r"|иә|жарайды|мақұл|растаймын|қос|қосыңыз|қосшы|қосыңызшы"
    r")(?![\w-])",
    re.IGNORECASE,
)
# Отказ или оговорка отменяют согласие: «нет», «не надо», «да, но 2 штуки».
NEGATION_RE = re.compile(
    r"(?<![\w-])(?:"
    r"не|нет|неа|но|если|только|при|после|когда|отмена|отмени|отмените|передумал|передумала|стоп|погоди|погодите"
    r"|подожди|подождите|жоқ|емес|бірақ|егер|тек|кейін|қоспа|қоспаңыз|болмайды"
    r")(?![\w-])",
    re.IGNORECASE,
)
# Согласие с дополнительными условиями требует уточнения, а не угадывания моделью.
CONFIRMATION_WORDS = frozenset(
    "да ага угу ок окей подтверждаю подтверждаем согласен согласна добавь добавьте "
    "добавляй добавляйте пожалуйста спасибо всё все товары позиции предложенные "
    "в корзину с предложением добавление целиком список шт штук штуки штука дана ед "
    "иә жарайды мақұл растаймын қос қосыңыз қосшы қосыңызшы себетке бәрін барлығын барлық".split()
)
QUANTITY_RE = re.compile(r"(?<![\w-])(\d+)(?![\w-])")

_lock = threading.Lock()
_carts: dict[str, dict] = {}


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or DEFAULT_PUBLIC_BASE_URL


def cart_url() -> str:
    return f"{public_base_url()}/cart"


def is_confirmation(message: object) -> bool:
    """Детерминированная проверка явного согласия покупателя в его сообщении."""
    if not isinstance(message, str):
        return False
    text = message.strip()
    if (
        not text or len(text) > 300
        or re.search(r"[^\w\s,.!;:—–-]", text)
        or re.search(r"[-−]\s*\d|\d\s*[-+*/]\s*\d", text)
        or len(QUANTITY_RE.findall(text)) > 1
    ):
        return False
    tokens = re.findall(r"\w+", text.casefold())
    return (
        bool(CONFIRM_RE.search(text)) and not NEGATION_RE.search(text)
        and all(token in CONFIRMATION_WORDS or token.isdecimal() for token in tokens)
    )


def confirmed_quantities(message: str) -> set[int]:
    """Числа, включая «да, 2» без единицы измерения."""
    return {int(number) for number in QUANTITY_RE.findall(message)}


def _empty_cart() -> dict:
    return {"items": {}, "pending": None, "turn": 0, "updated": time.monotonic()}


def _prune() -> None:
    now = time.monotonic()
    stale = [key for key, cart in _carts.items() if now - cart["updated"] > SESSION_TTL]
    for key in stale:
        del _carts[key]
    if len(_carts) >= MAX_SESSIONS:
        oldest = sorted(_carts, key=lambda key: _carts[key]["updated"])
        for key in oldest[: len(_carts) - MAX_SESSIONS + 1]:
            del _carts[key]


def new_session() -> str:
    """Создаёт пустую корзину и возвращает её секретный идентификатор."""
    with _lock:
        _prune()
        session_id = secrets.token_urlsafe(32)
        while session_id in _carts:
            session_id = secrets.token_urlsafe(32)
        _carts[session_id] = _empty_cart()
        return session_id


def is_known_session(session_id: object) -> bool:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return False
    with _lock:
        return session_id in _carts


def _cart(session_id: object) -> dict | None:
    """Корзина по идентификатору; вызывать под _lock."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    cart = _carts.get(session_id)
    if cart is not None:
        cart["updated"] = time.monotonic()
    return cart


def begin_turn(session_id: object) -> int:
    """Отмечает новое сообщение покупателя; возвращает его номер."""
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return 0
        cart["turn"] += 1
        return cart["turn"]


def end_turn(session_id: object, completed: bool) -> None:
    """Если ответ ассистента не дошёл до покупателя, он не видел предложения."""
    if completed:
        return
    with _lock:
        cart = _cart(session_id)
        if cart is not None and cart["pending"] and cart["pending"]["turn"] == cart["turn"]:
            cart["pending"] = None


def _item_total(item: dict) -> float | int | None:
    price = item.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return price * item["qty"]


def snapshot(session_id: object) -> dict:
    """Состав корзины для страницы, API и инструмента get_cart."""
    with _lock:
        cart = _cart(session_id)
        items = copy.deepcopy(cart["items"]) if cart else {}
        pending = copy.deepcopy(cart["pending"]) if cart else None
        # Подтвердить можно только предложение из предыдущего сообщения.
        awaiting = bool(pending) and pending["turn"] == cart["turn"] - 1
    rows = []
    total = 0
    for product_id, item in items.items():
        line_total = _item_total(item)
        if line_total is not None:
            total += line_total
        rows.append({
            "product_id": product_id,
            "article": item.get("article"),
            "name": item.get("name"),
            "qty": item["qty"],
            "price": item.get("price"),
            "total": line_total,
            "url": item.get("url"),
        })
    if pending:
        proposal_rows = [
            {key: row[key] for key in ("product_id", "article", "name", "price", "quantity", "total")}
            for row in pending["items"]
        ]
        pending = {
            "items": proposal_rows,
            "awaiting_confirmation": awaiting,
        }
        if len(proposal_rows) == 1:
            pending.update(proposal_rows[0])
    return {
        "items": rows,
        "count": len(rows),
        "total_quantity": sum(row["qty"] for row in rows),
        "total": total,
        "pending": pending,
        "cart_url": cart_url(),
    }


def quantity_in_cart(session_id: object, product_id: int) -> int:
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return 0
        item = cart["items"].get(product_id)
        return item["qty"] if item else 0


def set_pending(session_id: object, proposal: dict) -> bool:
    """Собирает позиции одного хода; повторный id заменяет количество."""
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return False
        pending = cart["pending"]
        if pending is None or pending["turn"] != cart["turn"]:
            pending = {"items": [], "turn": cart["turn"]}
        rows = {row["product_id"]: row for row in pending["items"]}
        if proposal["product_id"] not in rows and len(rows) >= MAX_PENDING_ITEMS:
            return False
        rows[proposal["product_id"]] = copy.deepcopy(proposal)
        cart["pending"] = {"items": list(rows.values()), "turn": cart["turn"]}
        return True


def reset_proposal_item(session_id: object, product_id: int) -> None:
    """Новое предложение заменяет прежний ход; невалидная правка убирает старую строку."""
    with _lock:
        state = _cart(session_id)
        if state is None or state["pending"] is None:
            return
        pending = state["pending"]
        if pending["turn"] != state["turn"]:
            state["pending"] = None
            return
        pending["items"] = [row for row in pending["items"] if row["product_id"] != product_id]
        if not pending["items"]:
            state["pending"] = None


def get_pending(session_id: object) -> dict | None:
    with _lock:
        cart = _cart(session_id)
        return copy.deepcopy(cart["pending"]) if cart else None


def clear_pending(session_id: object) -> bool:
    """Возвращает True, если предложение было и теперь снято."""
    with _lock:
        cart = _cart(session_id)
        if cart is None or cart["pending"] is None:
            return False
        cart["pending"] = None
        return True


def commit_pending(
    session_id: object, message: str, stock: int | dict[int, int],
    expected_pending: dict | None = None,
) -> dict:
    """Атомарно переносит все позиции после согласия и проверки всех остатков.

    Возвращает {"items": добавленные позиции, "added": количество} или {"error": ...}.
    Для единственной позиции сохраняет поле item для обратной совместимости.
    Все проверки детерминированные и выполняются здесь, а не в промпте.
    """
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return {"error": "Корзина недоступна: нет сессии покупателя."}
        pending = cart["pending"]
        if pending is None:
            return {"error": "Нет ожидающего предложения. Сначала вызови propose_cart_add."}
        if expected_pending is not None and pending != expected_pending:
            return {"error": "Предложение изменилось во время проверки. Покажи новый список и запроси подтверждение."}
        if pending["turn"] == cart["turn"]:
            return {
                "error": "Предложение создано в этом же сообщении: покажи его покупателю, "
                "спроси «Подтвердите добавление?» и дождись его ответа. Ничего не добавлено."
            }
        if pending["turn"] != cart["turn"] - 1:
            cart["pending"] = None
            return {
                "error": "Предложение устарело: покупатель не подтвердил его сразу. "
                "Вызови propose_cart_add заново и снова спроси подтверждение."
            }
        if not is_confirmation(message):
            return {
                "error": "Нет явного подтверждения от покупателя в его последнем сообщении. "
                "Товар не добавлен. Спроси «Подтвердите добавление?» и дождись ответа «да»."
            }
        quantities = confirmed_quantities(message)
        proposals = pending["items"]
        if quantities and (len(proposals) != 1 or quantities != {proposals[0]["quantity"]}):
            return {
                "error": "В подтверждении названо другое количество. Товар не добавлен: "
                "вызови propose_cart_add с нужным количеством и снова спроси подтверждение."
            }
        stocks = stock if isinstance(stock, dict) else {proposals[0]["product_id"]: stock}
        for row in proposals:
            item = cart["items"].get(row["product_id"])
            already = item["qty"] if item else 0
            available = stocks.get(row["product_id"], 0)
            if already + row["quantity"] > available:
                cart["pending"] = None
                return {
                    "error": f"Остатка {row['article']} недостаточно: в корзине уже {already} шт., доступно ещё "
                    f"{max(available - already, 0)} шт. Ничего не добавлено. Предложи обновлённый список.",
                    "product_id": row["product_id"],
                    "max_quantity": max(available - already, 0),
                }
        added = []
        for row in proposals:
            item = cart["items"].setdefault(row["product_id"], {
                "qty": 0, **{key: row[key] for key in ("name", "article", "price", "url")},
            })
            item["qty"] += row["quantity"]
            added.append({"product_id": row["product_id"], **copy.deepcopy(item), "added": row["quantity"]})
        cart["pending"] = None
        result = {"items": added, "added": sum(row["added"] for row in added)}
        if len(added) == 1:
            result["item"] = added[0]
        return result
