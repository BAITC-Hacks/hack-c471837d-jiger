"""Корзина покупателя: in-memory хранилище, привязанное к cookie сессии.

Схема хранения (всё под одним threading.Lock, потому что FastAPI выполняет
синхронные обработчики в пуле потоков):

    session_id -> {
        "items": {product_id: {"qty", "name", "article", "price", "url"}},
        "pending": предложение добавления | None,
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
    r"не|нет|неа|но|отмена|отмени|отмените|передумал|передумала|стоп|погоди|погодите"
    r"|подожди|подождите|жоқ|емес|бірақ|қоспа|қоспаңыз|болмайды"
    r")(?![\w-])",
    re.IGNORECASE,
)
# Количество в подтверждении («да, 2 шт») должно совпадать с предложением.
QUANTITY_RE = re.compile(r"(?<![\w-])(\d+)\s*(?:шт|штук|штуки|штука|дана|ед)(?![\w-])", re.IGNORECASE)

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
    if not text or len(text) > 300:
        return False
    return bool(CONFIRM_RE.search(text)) and not NEGATION_RE.search(text)


def confirmed_quantities(message: str) -> set[int]:
    """Количества вида «2 шт», названные в подтверждении."""
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
        pending = {
            **{key: pending[key] for key in ("product_id", "article", "name", "price", "quantity", "total")},
            "awaiting_confirmation": awaiting,
        }
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
    """Сохраняет предложение; товар в корзину НЕ попадает."""
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return False
        cart["pending"] = {**proposal, "turn": cart["turn"]}
        return True


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


def commit_pending(session_id: object, message: str, stock: int) -> dict:
    """Переносит предложение в корзину после явного подтверждения покупателя.

    Возвращает {"item": позиция корзины, "added": количество} или {"error": ...}.
    Все проверки детерминированные и выполняются здесь, а не в промпте.
    """
    with _lock:
        cart = _cart(session_id)
        if cart is None:
            return {"error": "Корзина недоступна: нет сессии покупателя."}
        pending = cart["pending"]
        if pending is None:
            return {"error": "Нет ожидающего предложения. Сначала вызови propose_cart_add."}
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
        if quantities and quantities != {pending["quantity"]}:
            return {
                "error": "В подтверждении названо другое количество. Товар не добавлен: "
                "вызови propose_cart_add с нужным количеством и снова спроси подтверждение."
            }
        item = cart["items"].get(pending["product_id"])
        already = item["qty"] if item else 0
        if already + pending["quantity"] > stock:
            cart["pending"] = None
            return {
                "error": f"Остатка недостаточно: в корзине уже {already} шт., доступно ещё "
                f"{max(stock - already, 0)} шт. Предложи покупателю доступное количество.",
                "max_quantity": max(stock - already, 0),
            }
        if item is None:
            item = {
                "qty": 0,
                "name": pending["name"],
                "article": pending["article"],
                "price": pending["price"],
                "url": pending["url"],
            }
            cart["items"][pending["product_id"]] = item
        item["qty"] += pending["quantity"]
        cart["pending"] = None
        return {"item": {"product_id": pending["product_id"], **copy.deepcopy(item)}, "added": pending["quantity"]}
