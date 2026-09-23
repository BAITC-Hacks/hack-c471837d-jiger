import argparse
import json
import logging
import os
import traceback
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

from app import analogs, cart, catalog, redact_secrets
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
                "Подобрать до пяти доступных альтернатив по точному артикулу товара. "
                "Возвращает товары той же категории с положительным остатком и reason. "
                "При неизвестном артикуле или категории возвращает пустой список. "
                "Результаты упорядочены по сходству описаний; совместимость нужно "
                "проверять по характеристикам."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string", "description": "Точный артикул исходного товара"}},
                "required": ["sku"],
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
                        "description": "Только товары с остатком больше нуля",
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
                        "description": "id товара из результатов search_products или get_product",
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
                "Нельзя вызывать в одном ответе с propose_cart_add. При успехе возвращает состав "
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


def propose_cart_add(session_id: str | None, product_id: int, quantity: int) -> dict:
    """Шаг 1: проверка товара, остатка и кратности; сохраняет предложение, не меняя корзину."""
    if type(product_id) is not int or product_id < 1:
        raise ValueError("product_id должен быть положительным целым числом.")
    if type(quantity) is not int or quantity < 1:
        raise ValueError("quantity должно быть целым числом не меньше 1.")
    if error := _no_session_error(session_id):
        return error
    product = catalog.find_product(product_id)
    if product is None:
        return {"error": f"Товар с id {product_id} не найден в загруженном каталоге."}
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
    cart.set_pending(session_id, proposal)
    result = {"status": "pending"}
    result.update({key: proposal[key] for key in ("article", "name", "price", "quantity", "total")})
    if already:
        result["already_in_cart"] = already
    result["note"] = (
        "Товар НЕ добавлен. Покажи покупателю артикул, название, цену, количество и сумму "
        "и спроси «Подтвердите добавление?». confirm_cart_add вызывай только после его ответа."
    )
    return result


def confirm_cart_add(session_id: str | None, user_message: str) -> dict:
    """Шаг 2: переносит предложение в корзину, если покупатель явно согласился."""
    if error := _no_session_error(session_id):
        return error
    pending = cart.get_pending(session_id)
    stock = 0
    if pending is not None:
        product = catalog.find_product(pending["product_id"])
        if product is None:
            cart.clear_pending(session_id)
            return {"error": "Товар из предложения больше не найден в каталоге. Предложение снято."}
        stock = catalog.stock_quantity(product)
    result = cart.commit_pending(session_id, user_message, stock)
    if "error" in result:
        return result
    item = result["item"]
    state = cart.snapshot(session_id)
    return {
        "status": "added",
        "article": item["article"],
        "name": item["name"],
        "added": result["added"],
        "qty_in_cart": item["qty"],
        "price": item["price"],
        "cart_count": state["count"],
        "cart_total": state["total"],
        "cart_url": state["cart_url"],
        "note": "Товар добавлен. Сообщи об этом покупателю и обязательно дай ссылку cart_url.",
    }


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
        lines.append(
            f"В прошлом ответе покупателю предложено добавить {pending['article']} — "
            f"{pending['name']} × {pending['quantity']} шт. и задан вопрос «Подтвердите добавление?». "
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


def log_exception(label: str) -> None:
    log(label, traceback.format_exc())


def finish(answer: str) -> str:
    answer = redact_secrets(answer)
    log("Ответ", answer)
    return answer


def execute_tool(
    name: str, arguments: str, session_id: str | None = None, user_message: str = ""
) -> object:
    """Выполняет вызов инструмента модели.

    session_id — идентификатор корзины из cookie (None вне HTTP-сессии: инструменты
    корзины недоступны); user_message — текущее сообщение покупателя, по которому
    сервер сам решает, было ли явное подтверждение добавления.
    """
    log("Инструмент", f"{name} {arguments}")
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
        if name == "find_analogs" and set(args) == {"sku"}:
            return find_analogs(**args)
        if name == "list_categories" and not args:
            return list_categories()
        if name == "propose_cart_add" and set(args) == {"product_id", "quantity"}:
            return propose_cart_add(session_id, **args)
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
) -> str:
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except (OSError, UnicodeError):
        return finish("Не удалось прочитать .env. Проверьте права доступа и кодировку UTF-8.")

    if not isinstance(message, str):
        return finish("Сообщение должно быть текстом. Напишите, какой товар вы ищете.")
    log("Запрос", message)
    if not isinstance(history, list):
        return finish("История разговора должна быть списком реплик user/assistant.")

    language_hint = ""
    if any(letter in message.casefold() for letter in "әғқңөұүһі"):
        language_hint = "\n\nТекущий вопрос на казахском. Жауапты қазақ тілінде бер."
    context_note = shown_products_note(product_ids or [])
    cart_note = cart_context_note(session_id)
    system_prompt = SYSTEM_PROMPT + "".join(
        "\n\n" + note for note in (context_note, cart_note) if note
    ) + language_hint
    messages = [{"role": "system", "content": system_prompt}]
    for entry in history[-10:]:
        if (
            not isinstance(entry, dict)
            or entry.get("role") not in ("user", "assistant")
            or not isinstance(entry.get("content"), str)
        ):
            return finish("В истории допустимы только текстовые реплики user/assistant.")
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": message.strip()})

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return finish("Не настроен OPENAI_API_KEY. Добавьте ключ в окружение или .env.")
    model = os.environ.get("MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    try:
        with OpenAI(api_key=api_key, timeout=30.0, max_retries=1) as client:
            # MAX_TOOL_ROUNDS раундов инструментов, затем только финальный текст модели.
            for iteration in range(MAX_TOOL_ROUNDS + 1):
                final_round = iteration == MAX_TOOL_ROUNDS
                if final_round:
                    messages.append({"role": "system", "content": FINAL_ROUND_NOTE})
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="none" if final_round else "auto",
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
        reply = _run_agent(message, history, products, product_ids, session_id, turn)
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
        run_agent(args.message, [])
    except KeyboardInterrupt:
        print("\nЗапрос прерван.")
