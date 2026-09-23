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

from app import catalog, redact_secrets
from app.prompts import SYSTEM_PROMPT

MAX_TOOL_ROUNDS = 4
ERROR_REPLY = "Извините, произошла ошибка. Попробуйте ещё раз."
OVERLOAD_REPLY = "Сервис перегружен, попробуйте через минуту"

# Отладочные логи SDK/HTTP могут содержать тело запроса; используем свои логи.
for logger_name in ("openai", "httpx", "httpcore"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Поиск реальных товаров по артикулу, названию и характеристикам в каталоге EKT. "
                "Артикул передавай целиком, сохраняя нули, дефисы и подчёркивания. "
                "Передавай короткий запрос на русском; при отсутствии совпадений "
                "попробуй синоним или более общий запрос."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Ключевые слова товара"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query", "max_results"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": (
                "Полные данные товара из кэша по id. Поле detail содержит исходный ответ "
                "карточки: описание, properties, quantity, stores и offers. "
                "Также доступны article, image, url и url_api_detail."
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
]


def search_products(query: str, max_results: int = 5) -> list[dict]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Для поиска нужны непустые ключевые слова.")
    if type(max_results) is not int or not 1 <= max_results <= 10:
        raise ValueError("max_results должен быть целым числом от 1 до 10.")
    return catalog.search_products(query, max_results)


def get_product(product_id: int) -> dict:
    if type(product_id) is not int or product_id < 1:
        raise ValueError("product_id должен быть положительным целым числом.")
    for product in catalog.load_catalog():
        if str(product.get("id")) == str(product_id):
            return product
    return {"error": f"Товар с id {product_id} не найден в загруженном каталоге."}


def log(label: str, value: str) -> None:
    print(redact_secrets(f"{label}: {value}"), flush=True)


def log_exception(label: str) -> None:
    log(label, traceback.format_exc())


def finish(answer: str) -> str:
    answer = redact_secrets(answer)
    log("Ответ", answer)
    return answer


def execute_tool(name: str, arguments: str) -> object:
    log("Инструмент", f"{name} {arguments}")
    try:
        args = json.loads(arguments)
        if not isinstance(args, dict):
            raise ValueError("Аргументы инструмента должны быть JSON-объектом.")
        if name == "search_products" and set(args) == {"query", "max_results"}:
            return search_products(**args)
        if name == "get_product" and set(args) == {"product_id"}:
            return get_product(**args)
        raise ValueError("Неизвестный инструмент или неверный набор аргументов.")
    except json.JSONDecodeError:
        return {"error": "Некорректный JSON аргументов. Исправь аргументы инструмента."}
    except (ValueError, RuntimeError) as error:
        return {"error": redact_secrets(str(error))}


def _run_agent(message: str, history: list[dict]) -> str:
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
    messages = [{"role": "system", "content": SYSTEM_PROMPT + language_hint}]
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
            # Четыре раунда инструментов, затем только финальный текст модели.
            for iteration in range(MAX_TOOL_ROUNDS + 1):
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="none" if iteration == MAX_TOOL_ROUNDS else "auto",
                    parallel_tool_calls=False,
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
                    return finish(answer)
                if iteration == MAX_TOOL_ROUNDS:
                    raise RuntimeError("Не удалось завершить подбор. Уточните запрос и повторите.")

                messages.append(reply.model_dump(exclude_none=True))
                for call in reply.tool_calls:
                    result = execute_tool(call.function.name, call.function.arguments)
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


def run_agent(message: str, history: list[dict]) -> str:
    try:
        return _run_agent(message, history)
    except Exception:
        log_exception("Необработанная ошибка run_agent")
        return finish(ERROR_REPLY)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Задать вопрос ИИ-консультанту EKT.")
    parser.add_argument("message", help="Вопрос покупателя в кавычках")
    args = parser.parse_args()
    try:
        run_agent(args.message, [])
    except KeyboardInterrupt:
        print("\nЗапрос прерван.")
