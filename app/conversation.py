"""Одинаковые границы истории для HTTP и прямых вызовов агента."""

MAX_HISTORY_MESSAGES = 10
MAX_HISTORY_MESSAGE_CHARS = 6000
MAX_HISTORY_CHARS = 20_000
MAX_JSON_BODY_BYTES = 128 * 1024


def validate_history(history: object) -> list[dict]:
    if not isinstance(history, list):
        raise ValueError("История разговора должна быть списком реплик user/assistant.")
    if len(history) > MAX_HISTORY_MESSAGES:
        raise ValueError("В истории допустимо не более 10 последних реплик.")
    cleaned = []
    total = 0
    for entry in history:
        if (
            not isinstance(entry, dict)
            or entry.get("role") not in ("user", "assistant")
            or not isinstance(entry.get("content"), str)
        ):
            raise ValueError("В истории допустимы только текстовые реплики user/assistant.")
        content = entry["content"]
        if len(content) > MAX_HISTORY_MESSAGE_CHARS:
            raise ValueError("Реплика в истории превышает лимит 6000 символов.")
        total += len(content)
        if total > MAX_HISTORY_CHARS:
            raise ValueError("История превышает общий лимит 20000 символов.")
        cleaned.append({"role": entry["role"], "content": content})
    return cleaned
