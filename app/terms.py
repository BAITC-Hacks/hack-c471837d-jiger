"""Условия покупки и контакты магазина из data/terms.md для системного промпта.

Файл — единственный источник ответов об оплате, доставке, минимальной партии
и контактах менеджеров; если его нет, модель обязана честно говорить, что
условия не заданы, и не выдумывать их.
"""

from pathlib import Path

TERMS_PATH = Path(__file__).resolve().parent.parent / "data" / "terms.md"
MAX_TERMS_CHARS = 6000
MISSING_TERMS_NOTE = (
    "Условия покупки и контакты магазина не заданы (файл data/terms.md отсутствует). "
    "На вопросы об оплате, доставке, минимальной партии, возврате и контактах честно "
    "отвечай, что не можешь их сообщить, и предложи обратиться в магазин через сайт ekt.kz. "
    "Ничего не выдумывай."
)

_cache: dict = {"key": None, "text": ""}


def load_terms() -> str:
    """Текст условий; пустая строка, если файла нет. Перечитывается при изменении файла."""
    try:
        stat = TERMS_PATH.stat()
    except OSError:
        return ""
    key = (str(TERMS_PATH), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] != key:
        try:
            text = TERMS_PATH.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
        _cache.update(key=key, text=text[:MAX_TERMS_CHARS])
    return _cache["text"]


def terms_note() -> str:
    """Блок для системного промпта: условия покупки и контакты или пометка об их отсутствии."""
    text = load_terms()
    if not text:
        return MISSING_TERMS_NOTE
    return (
        "Условия покупки и контакты магазина (единственный источник для вопросов об оплате, "
        "доставке, минимальной партии и сумме заказа, возврате, графике работы и контактах; "
        "чего здесь нет — не выдумывай, а предложи уточнить у менеджера):\n" + text
    )
