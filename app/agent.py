import argparse
import json
import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from app import analogs, cart, catalog, redact_secrets, stock, terms
from app.conversation import validate_history
from app.prompts import SYSTEM_PROMPT

MAX_TOOL_ROUNDS = 5
MAX_CONTEXT_PRODUCTS = 10
FINAL_ROUND_NOTE = (
    "Инструменты больше недоступны. Ответь по уже полученным данным; "
    "если что-то не проверено, честно скажи об этом и предложи уточнить запрос."
)
ERROR_REPLY = "Извините, произошла ошибка. Попробуйте ещё раз."
OVERLOAD_REPLY = "Сервис перегружен, попробуйте через минуту"
# Поля товара, которые уходят во фронтенд для карточек в чате.
PRODUCT_SUMMARY_FIELDS = ("id", "name", "article", "price", "url", "image", "quantity")

# Отладочные логи SDK/HTTP могут содержать тело запроса; используем свои логи.
for logger_name in ("openai", "httpx", "httpcore"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_analogs",
            "description": (
                "Подобрать аналоги по id найденного товара, в том числе при quantity=0. "
                "Только товары близкой группы с положительным остатком. Возвращает "
                "matched (совпавшие свойства и токены названия) и differs (отличия, "
                "неизвестные характеристики и бренд исходного товара/аналога). "
                "Покажи 1–3 результата и объясни совпадения и отличия; сходство "
                "не гарантирует совместимость. При отсутствии аналогов вернёт []."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer", "minimum": 1, "description": "id исходного товара из каталога"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["product_id", "max_results"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Поиск реальных товаров в каталоге EKT по словам с фильтрами. "
                "Артикул передавай целиком, сохраняя нули, дефисы и подчёркивания. "
                "При запросе конкретного артикула ВСЕГДА in_stock_only=false, даже "
                "на вопрос «есть в наличии?»: сначала найди товар, затем проверь quantity. "
                "При quantity=0 вызови find_analogs с id найденного товара. "
                "Неизвестный точный артикул означает, что товар не найден; не сокращай его. "
                "query — короткие ключевые слова на русском; может быть пустой строкой, "
                "если задан хотя бы один фильтр (тогда подбираются все товары под фильтры). "
                "Для «до N тенге», «в районе N», «дешевле» используй min_price/max_price. "
                "Для «покажи ещё» повтори тот же запрос с offset = число уже показанных. "
                "Ответ: total (сколько всего подходит), offset и items; каждый товар содержит "
                "price, quantity, in_stock, stores (склады с остатком) и category. "
                "Товары в наличии идут первыми. При отсутствии совпадений попробуй "
                "синоним, более общий запрос или list_categories."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Ключевые слова или пустая строка"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    "in_stock_only": {
                        "type": "boolean",
                        "description": (
                            "true — только товары с остатком для общей подборки. "
                            "Для конкретного артикула ВСЕГДА false, чтобы увидеть quantity=0 "
                            "и предложить аналоги через find_analogs."
                        ),
                    },
                    "min_price": {
                        "type": ["number", "null"],
                        "description": "Минимальная цена в тенге или null",
                    },
                    "max_price": {
                        "type": ["number", "null"],
                        "description": "Максимальная цена в тенге или null",
                    },
                    "category": {
                        "type": ["string", "null"],
                        "description": "Часть названия раздела из list_categories или null",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Сколько первых результатов пропустить (для «показать ещё»)",
                    },
                },
                "required": [
                    "query", "max_results", "in_stock_only", "min_price", "max_price", "category", "offset",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": (
                "Разделы загруженного каталога с числом товаров и числом товаров в наличии. "
                "Вызывай, когда запрошенной категории не нашлось или покупатель спрашивает, "
                "что вообще есть в магазине, чтобы предложить реальные разделы."
            ),
            "strict": True,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": (
                "Полная карточка товара по id: описание, properties (характеристики), "
                "min_batch (минимальная партия), quantity (общий остаток) и stores — "
                "склады с остатком больше нуля; склады, которых нет в stores, имеют остаток 0. "
                "Для нескольких товаров вызывай параллельно в одном раунде."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer", "minimum": 1}},
                "required": ["product_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_cart_add",
            "description": (
                "Шаг 1 добавления в корзину: подготовить предложение «товар × количество». "
                "Для нескольких товаров вызови для каждого: позиции одного сообщения объединяются "
                "в общий список; повтор того же product_id заменяет его количество. "
                "Сервер проверяет товар в каталоге, остаток и кратность продажи и сохраняет "
                "предложение; в корзину при этом НИЧЕГО не добавляется. Покажи покупателю "
                "артикул, название, цену, количество и сумму из ответа и спроси "
                "«Подтвердите добавление?». При ошибке используй max_quantity или "
                "suggested_quantity: предложи доступное количество и снова спроси подтверждение."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Поле id товара из результатов search_products или get_product в этом "
                            "диалоге. Артикул и цифры из артикула не подходят: сначала найди товар."
                        ),
                    },
                    "quantity": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Сколько штук хочет покупатель",
                    },
                },
                "required": ["product_id", "quantity"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_cart_add",
            "description": (
                "Шаг 2 добавления в корзину: вызывай ТОЛЬКО когда покупатель в своём последнем "
                "сообщении явно согласился с предложением («да», «ок», «подтверждаю», «добавьте», "
                "«иә», «жарайды»). Сервер сам проверяет согласие в сообщении и отказывает без него. "
                "Нельзя вызывать в одном ответе с propose_cart_add. Подтверждает ВЕСЬ предложенный "
                "список атомарно: при недостаточном остатке любой позиции ничего не добавляется. "
                "Согласие с условием или изменением количества требует нового предложения. При успехе возвращает состав "
                "корзины и cart_url — обязательно дай эту ссылку покупателю."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_cart_proposal",
            "description": (
                "Снять неподтверждённое предложение добавления, если покупатель отказался "
                "или передумал. Состав корзины не меняет."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cart",
            "description": (
                "Текущий состав корзины покупателя: позиции, количество, суммы и cart_url. "
                "Ничего не изменяет. Вызывай на вопросы о корзине вместо догадок."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def find_analogs(sku: str) -> list[dict]:
    return analogs.find_analogs(sku)


def search_products(
    query: str,
    max_results: int = 10,
    in_stock_only: bool = False,
    min_price: float | None = None,
    max_price: float | None = None,
    category: str | None = None,
    offset: int = 0,
    *,
    return_page: bool = False,
) -> list[dict] | dict:
    if not isinstance(query, str):
        raise ValueError("query должен быть строкой (можно пустой при фильтрах).")
    if type(max_results) is not int or not 1 <= max_results <= catalog.MAX_SEARCH_RESULTS:
        raise ValueError(f"max_results должен быть целым числом от 1 до {catalog.MAX_SEARCH_RESULTS}.")
    if not isinstance(in_stock_only, bool):
        raise ValueError("in_stock_only должен быть true или false.")
    result = catalog.search_products(
        query, max_results, in_stock_only=in_stock_only, min_price=min_price,
        max_price=max_price, category=category, offset=offset,
    )
    # Прямые вызовы сохраняют список; инструменту чата нужны total и offset.
    return result if return_page else result["items"]


def list_categories() -> list[dict]:
    return catalog.list_categories()


def get_product(product_id: int) -> dict:
    if type(product_id) is not int or product_id < 1:
        raise ValueError("product_id должен быть положительным целым числом.")
    product = catalog.find_product(product_id)
    if product is None:
        return {"error": f"Товар с id {product_id} не найден в загруженном каталоге."}
    return {**catalog.product_card(product), "detail": product.get("detail", product)}


def _no_session_error(session_id: str | None) -> dict | None:
    if not cart.is_known_session(session_id):
        return {"error": "Корзина недоступна: нет сессии покупателя (cookie ekt_session)."}
    return None


def product_from_message(message: str) -> dict | None:
    """Единственный товар, чей артикул покупатель назвал в сообщении, иначе None."""
    if not isinstance(message, str):
        return None
    folded = message.casefold()
    candidates = [folded.strip()] + catalog.SEARCH_TOKEN.findall(folded)
    found: dict[int, dict] = {}
    for token in candidates:
        # Короткие или чисто буквенные токены не считаем артикулами.
        if len(token) < 5 or token.isalpha():
            continue
        product = catalog.find_by_article(token)
        if product is not None:
            found[id(product)] = product
    return next(iter(found.values())) if len(found) == 1 else None


def propose_cart_add(
    session_id: str | None, product_id: int, quantity: int, user_message: str = ""
) -> dict:
    """Шаг 1: проверка товара, остатка и кратности; сохраняет предложение, не меняя корзину."""
    if type(product_id) is not int or product_id < 1:
        raise ValueError("product_id должен быть положительным целым числом.")
    if type(quantity) is not int or quantity < 1:
        raise ValueError("quantity должно быть целым числом не меньше 1.")
    if error := _no_session_error(session_id):
        return error
    cart.reset_proposal_item(session_id, product_id)
    product = catalog.find_product(product_id)
    if product is None:
        error = {
            "error": (
                f"Товар с id {product_id} не найден в загруженном каталоге. product_id — это поле id "
                "из результатов search_products или get_product, а не артикул."
            )
        }
        hinted = product_from_message(user_message)
        if hinted is not None:
            fields = catalog.product_fields(hinted)
            error["hint"] = {key: fields.get(key) for key in ("id", "article", "name", "quantity")}
            error["error"] += (
                f" В сообщении покупателя указан артикул {fields.get('article')} — это товар "
                f"id {fields.get('id')}: вызови propose_cart_add с этим id."
            )
        return error
    fields = catalog.product_fields(product)
    stock = catalog.stock_quantity(product)
    batch = catalog.min_batch(product)
    already = cart.quantity_in_cart(session_id, product_id)
    # Наибольшее количество, которое ещё можно добавить с учётом кратности.
    available = max(stock - already, 0)
    available -= available % batch
    if available < 1:
        if stock < 1:
            message = (
                "Товара нет в наличии по данным каталога: добавить нельзя. "
                "Предложи покупателю аналог из результатов инструментов."
            )
        elif already >= stock:
            message = f"Весь остаток ({stock} шт.) уже в корзине покупателя."
        else:
            message = f"Доступный остаток {stock - already} шт. меньше минимальной партии {batch} шт."
        return {"error": message, "max_quantity": 0, "min_batch": batch}
    if quantity > available:
        return {
            "error": (
                f"Доступно только {available} шт. (остаток {stock} шт., в корзине уже {already} шт.). "
                f"Предложи покупателю {available} шт. и снова спроси подтверждение."
            ),
            "max_quantity": available,
            "min_batch": batch,
        }
    if quantity % batch:
        suggested = max(quantity - quantity % batch, batch)
        return {
            "error": (
                f"Товар продаётся кратно {batch} шт. Предложи покупателю {suggested} шт. "
                "и снова спроси подтверждение."
            ),
            "min_batch": batch,
            "suggested_quantity": suggested,
            "max_quantity": available,
        }
    price = fields.get("price")
    total = price * quantity if isinstance(price, (int, float)) and not isinstance(price, bool) else None
    proposal = {
        "product_id": product_id,
        "article": fields.get("article"),
        "name": fields.get("name"),
        "price": price,
        "url": fields.get("url"),
        "quantity": quantity,
        "total": total,
    }
    if not cart.set_pending(session_id, proposal):
        return {"error": f"Можно предложить не более {cart.MAX_PENDING_ITEMS} позиций за один раз. Раздели список."}
    result = {"status": "pending"}
    result.update({key: proposal[key] for key in ("article", "name", "price", "quantity", "total")})
    if already:
        result["already_in_cart"] = already
    result["items"] = cart.snapshot(session_id)["pending"]["items"]
    result["note"] = (
        "Товары НЕ добавлены. Покажи покупателю ВСЕ позиции из items: артикул, название, цену, "
        "количество и сумму; спроси одним предложением «Подтвердите добавление всех позиций?». "
        "confirm_cart_add вызывай только после его ответа."
    )
    return result


def confirm_cart_add(session_id: str | None, user_message: str) -> dict:
    """Шаг 2: проверяет все позиции и добавляет их вместе после согласия."""
    if error := _no_session_error(session_id):
        return error
    pending = cart.get_pending(session_id)
    if pending is None or not cart.is_confirmation(user_message):
        return cart.commit_pending(session_id, user_message, {})
    quantities = cart.confirmed_quantities(user_message)
    if quantities and (len(pending["items"]) != 1 or quantities != {pending["items"][0]["quantity"]}):
        return cart.commit_pending(session_id, user_message, {}, expected_pending=pending)
    products = []
    for proposal in pending["items"]:
        product = catalog.find_product(proposal["product_id"])
        if product is None:
            cart.clear_pending(session_id)
            return {"error": "Товар из предложения больше не найден в каталоге. Предложение снято."}
        if proposal["quantity"] % catalog.min_batch(product):
            cart.clear_pending(session_id)
            return {"error": "Кратность продажи изменилась. Ничего не добавлено: предложи обновлённый список."}
        products.append(product)

    def refresh_stock(product: dict) -> tuple[int, str, bool]:
        # Перед добавлением остаток сверяется с API ekt.kz; без ответа API — по кэшу.
        attempted = stock.check_available(product)
        live = stock.live_quantity(product) if attempted else None
        if live is not None:
            return live, "live", False
        return catalog.stock_quantity(product), "cache", attempted

    if len(products) == 1:
        checks = [refresh_stock(products[0])]
    else:
        with ThreadPoolExecutor(max_workers=5) as pool:
            checks = list(pool.map(refresh_stock, products))
    available = {row["product_id"]: check[0] for row, check in zip(pending["items"], checks)}
    sources = {check[1] for check in checks}
    stock_source = sources.pop() if len(sources) == 1 else "mixed"
    stock_note = (
        "Свежий остаток из API ekt.kz для части позиций получить не удалось: количество проверено "
        "по данным кэша каталога. Скажи об этом покупателю."
    ) if any(check[2] for check in checks) else ""
    result = cart.commit_pending(session_id, user_message, available, expected_pending=pending)
    if "error" in result:
        if stock_note:
            result["stock_note"] = stock_note
        return result
    state = cart.snapshot(session_id)
    added_items = result["items"]
    response = {
        "status": "added",
        "items": added_items,
        "added": result["added"],
        "cart_count": state["count"],
        "cart_total": state["total"],
        "cart_url": state["cart_url"],
        "stock_source": stock_source,
        "note": "Все предложенные позиции добавлены. Сообщи об этом покупателю и обязательно дай ссылку cart_url.",
    }
    if len(added_items) == 1:
        item = added_items[0]
        response.update({"article": item["article"], "name": item["name"], "qty_in_cart": item["qty"], "price": item["price"]})
    if stock_note:
        response["stock_note"] = stock_note
    return response


def cancel_cart_proposal(session_id: str | None) -> dict:
    if error := _no_session_error(session_id):
        return error
    return {"status": "cancelled" if cart.clear_pending(session_id) else "nothing_pending"}


def get_cart(session_id: str | None) -> dict:
    if error := _no_session_error(session_id):
        return error
    return cart.snapshot(session_id)


def cart_context_note(session_id: str | None) -> str:
    """Блок для системного промпта: состав корзины и предложение, ждущее ответа покупателя."""
    if not cart.is_known_session(session_id):
        return ""
    state = cart.snapshot(session_id)
    if state["items"]:
        rows = "; ".join(
            f"{row['article']} — {row['name']} × {row['qty']} шт." for row in state["items"]
        )
        lines = [f"Корзина покупателя сейчас: {rows}. Ссылка на корзину: {state['cart_url']}"]
    else:
        lines = [f"Корзина покупателя пуста. Ссылка на корзину: {state['cart_url']}"]
    pending = state["pending"]
    if pending and pending["awaiting_confirmation"]:
        proposed = "; ".join(
            f"{row['article']} — {row['name']} × {row['quantity']} шт."
            for row in pending["items"]
        )
        lines.append(
            f"В прошлом ответе покупателю предложено добавить ВЕСЬ список: {proposed}. "
            "Задан вопрос «Подтвердите добавление всех позиций?». "
            "Если текущее сообщение — явное согласие, вызови confirm_cart_add; если отказ — "
            "cancel_cart_proposal; если покупатель хочет другое количество или другой товар — "
            "propose_cart_add заново."
        )
    return "\n".join(lines)


def shown_products_note(product_ids: list) -> str:
    """Блок для системного промпта: товары, уже показанные покупателю в этом диалоге."""
    wanted = []
    for product_id in product_ids:
        if type(product_id) is int and product_id > 0 and product_id not in wanted:
            wanted.append(product_id)
    wanted = wanted[-MAX_CONTEXT_PRODUCTS:]
    if not wanted:
        return ""
    by_id = {str(product.get("id")): product for product in catalog.load_catalog()}
    lines = []
    for product_id in wanted:
        product = by_id.get(str(product_id))
        if product is None:
            continue
        fields = catalog.product_fields(product)
        stores = ", ".join(
            f"{store['name']} ({store['quantity']} шт.)" for store in fields["stores"]
        ) or "остатков на складах нет"
        lines.append(
            f"- id {fields.get('id')}: {fields.get('name')} — артикул {fields.get('article')} — "
            f"цена {fields.get('price')} — остаток {fields.get('quantity')} — склады: {stores}"
        )
    if not lines:
        return ""
    return (
        "Товары, уже показанные покупателю в этом диалоге (данные каталога):\n" + "\n".join(lines)
        + "\nИспользуй их только для уточняющих вопросов об этих позициях («эти», «их», "
        "«первый/второй», наличие, характеристики); для подробностей вызывай get_product по id. "
        "Если покупатель просит другие, ещё, дешевле, дороже, в наличии или иную категорию — "
        "делай новый search_products с нужными фильтрами и не повторяй эти позиции."
    )


def remember_products(store: dict[str, dict], result: object) -> None:
    """Запоминает товары из результатов инструментов для карточек в интерфейсе."""
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        result = result["items"]
    items = result if isinstance(result, list) else [result]
    for item in items:
        if not isinstance(item, dict) or "id" not in item or "error" in item:
            continue
        fields = catalog.product_fields(item)
        store[str(item["id"])] = {key: fields.get(key) for key in PRODUCT_SUMMARY_FIELDS}


def mentioned_products(store: dict[str, dict], reply: str) -> list[dict]:
    """Оставляет только товары, на которые ответ ссылается по url или названию."""
    folded = reply.casefold()
    mentioned = []
    for product in store.values():
        url = str(product.get("url") or "").rstrip("/").casefold()
        name = str(product.get("name") or "").casefold()
        if (url and url in folded) or (name and name in folded):
            mentioned.append(product)
    return mentioned


def log(label: str, value: str) -> None:
    print(redact_secrets(f"{label}: {value}"), flush=True)


def content_logging_enabled() -> bool:
    """Текст сообщений покупателя и ответов попадает в лог только при LOG_LEVEL=DEBUG."""
    return os.environ.get("LOG_LEVEL", "").strip().upper() == "DEBUG"


def log_exception(label: str) -> None:
    log(label, traceback.format_exc())


def finish(answer: str) -> str:
    answer = redact_secrets(answer)
    log("Ответ", answer if content_logging_enabled() else f"{len(answer)} символов")
    return answer


def execute_tool(
    name: str, arguments: str, session_id: str | None = None, user_message: str = ""
) -> object:
    """Выполняет вызов инструмента модели.

    session_id — идентификатор корзины из cookie (None вне HTTP-сессии: инструменты
    корзины недоступны); user_message — текущее сообщение покупателя, по которому
    сервер сам решает, было ли явное подтверждение добавления.
    """
    # Аргументы могут содержать текст покупателя: по умолчанию логируется только имя.
    log("Инструмент", f"{name} {arguments}" if content_logging_enabled() else name)
    try:
        args = json.loads(arguments)
        if not isinstance(args, dict):
            raise ValueError("Аргументы инструмента должны быть JSON-объектом.")
        if name == "search_products" and set(args) == {
            "query", "max_results", "in_stock_only", "min_price", "max_price", "category", "offset",
        }:
            return search_products(**args, return_page=True)
        if name == "get_product" and set(args) == {"product_id"}:
            return get_product(**args)
        if name == "find_analogs":
            if set(args) == {"product_id", "max_results"}:
                return catalog.find_analogs(**args)
            # Совместимость с прежними прямыми вызовами инструмента по артикулу.
            if set(args) == {"sku"}:
                return find_analogs(**args)
        if name == "list_categories" and not args:
            return list_categories()
        if name == "propose_cart_add" and set(args) == {"product_id", "quantity"}:
            return propose_cart_add(session_id, **args, user_message=user_message)
        if name == "confirm_cart_add" and not args:
            return confirm_cart_add(session_id, user_message)
        if name == "cancel_cart_proposal" and not args:
            return cancel_cart_proposal(session_id)
        if name == "get_cart" and not args:
            return get_cart(session_id)
        raise ValueError("Неизвестный инструмент или неверный набор аргументов.")
    except json.JSONDecodeError:
        return {"error": "Некорректный JSON аргументов. Исправь аргументы инструмента."}
    except (ValueError, RuntimeError) as error:
        return {"error": redact_secrets(str(error))}


def _run_agent(
    message: str,
    history: list[dict],
    products: dict[str, dict],
    product_ids: list | None = None,
    session_id: str | None = None,
    turn: dict | None = None,
    attachment_parts: list[dict] | None = None,
) -> str:
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except (OSError, UnicodeError):
        return finish("Не удалось прочитать .env. Проверьте права доступа и кодировку UTF-8.")

    if not isinstance(message, str):
        return finish("Сообщение должно быть текстом. Напишите, какой товар вы ищете.")
    try:
        history = validate_history(history)
    except ValueError as error:
        return finish(str(error))
    log(
        "Запрос",
        message if content_logging_enabled()
        else f"{len(message)} символов, история: {len(history)} реплик",
    )

    language_hint = ""
    if any(letter in message.casefold() for letter in "әғқңөұүһі"):
        language_hint = "\n\nТекущий вопрос на казахском. Жауапты қазақ тілінде бер."
    context_note = shown_products_note(product_ids or [])
    cart_note = cart_context_note(session_id)
    system_prompt = SYSTEM_PROMPT + "".join(
        "\n\n" + note for note in (terms.terms_note(), context_note, cart_note) if note
    ) + language_hint
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    content = message.strip()
    if attachment_parts:
        content = ([{"type": "text", "text": content}] if content else []) + attachment_parts
    messages.append({"role": "user", "content": content})

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return finish("Не настроен OPENAI_API_KEY. Добавьте ключ в окружение или .env.")
    model = os.environ.get("MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    pending_analogs: set[int] = set()
    checked_analogs: set[int] = set()

    try:
        with OpenAI(api_key=api_key, timeout=30.0, max_retries=1) as client:
            # MAX_TOOL_ROUNDS раундов инструментов, затем только финальный текст модели.
            for iteration in range(MAX_TOOL_ROUNDS + 1):
                final_round = iteration == MAX_TOOL_ROUNDS
                tool_choice = "none" if final_round else "auto"
                if pending_analogs and not final_round:
                    messages.append({"role": "system", "content": (
                        f"Каталог подтвердил нулевой остаток у id {sorted(pending_analogs)}. "
                        "Сейчас вызови find_analogs для этих product_id с max_results=3; "
                        "не предлагай покупателю самому просить поиск аналогов."
                    )})
                    tool_choice = {"type": "function", "function": {"name": "find_analogs"}}
                if checked_analogs:
                    messages.append({"role": "system", "content": (
                        "Для исходного товара скажи «нет в наличии» на языке покупателя. "
                        "Аналоги уже проверены: предложи 1–3 из результата find_analogs "
                        "с ценой, совпадениями и отличиями. Если результат пуст — предложи менеджера."
                    )})
                if final_round:
                    messages.append({"role": "system", "content": FINAL_ROUND_NOTE})
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice=tool_choice,
                    # Несколько get_product за один раунд: наличие по нескольким товарам.
                    parallel_tool_calls=True,
                    max_completion_tokens=800,
                )
                log("OpenAI", f"модель={model}, итерация={iteration + 1}, ответ={response.id}")
                if not response.choices:
                    raise RuntimeError("OpenAI не вернул ответ. Попробуйте ещё раз.")
                reply = response.choices[0].message
                if not reply.tool_calls:
                    answer = (reply.content or reply.refusal or "").strip()
                    if not answer:
                        raise RuntimeError("OpenAI вернул пустой ответ. Попробуйте ещё раз.")
                    if turn is not None:
                        # Ответ дойдёт до покупателя: предложение корзины можно подтверждать.
                        turn["completed"] = True
                    return finish(answer)
                if final_round:
                    raise RuntimeError("Не удалось завершить подбор. Уточните запрос и повторите.")

                messages.append(reply.model_dump(exclude_none=True))
                for call in reply.tool_calls:
                    result = execute_tool(
                        call.function.name, call.function.arguments, session_id, message
                    )
                    if call.function.name in ("search_products", "get_product"):
                        items = result.get("items", [result]) if isinstance(result, dict) else result
                        if isinstance(items, list):
                            pending_analogs.update(
                                item["id"] for item in items if isinstance(item, dict)
                                and type(item.get("id")) is int and item.get("quantity") == 0
                                and item["id"] not in checked_analogs
                            )
                    elif call.function.name == "find_analogs" and isinstance(result, list):
                        source_id = json.loads(call.function.arguments).get("product_id")
                        if type(source_id) is int:
                            checked_analogs.add(source_id)
                            pending_analogs.discard(source_id)
                    remember_products(products, result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
    except AuthenticationError:
        log_exception("Ошибка авторизации OpenAI")
        return finish("OpenAI отклонил ключ API. Проверьте OPENAI_API_KEY в .env.")
    except (RateLimitError, APIConnectionError):
        # APITimeoutError также является APIConnectionError; SDK уже сделал ретрай.
        log_exception("OpenAI недоступен после повторной попытки")
        return finish(OVERLOAD_REPLY)
    except APIStatusError as error:
        log_exception("Ошибка HTTP OpenAI")
        return finish(OVERLOAD_REPLY if error.status_code >= 500 else ERROR_REPLY)
    except APIError:
        log_exception("Ошибка ответа OpenAI")
        return finish(ERROR_REPLY)


def run_agent(
    message: str,
    history: list[dict],
    product_ids: list | None = None,
    session_id: str | None = None,
    attachment_parts: list[dict] | None = None,
) -> dict:
    """Возвращает {"reply": текст ответа, "products": товары для карточек}.

    product_ids — id товаров, уже показанных покупателю: они попадают в контекст
    модели, чтобы уточняющие вопросы («в каких городах есть?») не требовали
    повторного поиска. session_id — корзина покупателя из cookie; без него
    инструменты корзины недоступны.
    """
    products: dict[str, dict] = {}
    turn = {"completed": False}
    cart.begin_turn(session_id)
    try:
        reply = _run_agent(message, history, products, product_ids, session_id, turn, attachment_parts)
    except Exception:
        log_exception("Необработанная ошибка run_agent")
        reply = finish(ERROR_REPLY)
    # Если ответ не дошёл до покупателя, предложение этого хода снимается.
    cart.end_turn(session_id, turn["completed"])
    return {"reply": reply, "products": mentioned_products(products, reply)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Задать вопрос ИИ-консультанту EKT.")
    parser.add_argument("message", help="Вопрос покупателя в кавычках")
    args = parser.parse_args()
    try:
        # Лог по умолчанию не содержит текста ответа, поэтому печатаем его отдельно.
        print(run_agent(args.message, [])["reply"])
    except KeyboardInterrupt:
        print("\nЗапрос прерван.")
