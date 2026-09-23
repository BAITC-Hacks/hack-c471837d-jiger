"""Свежий остаток товара из API ekt.kz перед подтверждением добавления в корзину.

Запрос выполняется только при заданных EKT_API_USER и EKT_API_PASSWORD, по ссылке
url_api_detail из кэша каталога и с коротким таймаутом. Любая ошибка сети или
формата даёт None: тогда используется остаток из кэша, о чём сообщается покупателю.
"""

import math
import os

import httpx

from app import catalog

LIVE_TIMEOUT = 5.0


def credentials() -> tuple[str, str] | None:
    user = os.environ.get("EKT_API_USER", "").strip()
    password = os.environ.get("EKT_API_PASSWORD", "").strip()
    return (user, password) if user and password else None


def detail_url(product: dict) -> str | None:
    """Ссылка на карточку API только исходного сервера EKT (Basic Auth уходит лишь туда)."""
    detail = product.get("detail") if isinstance(product.get("detail"), dict) else {}
    link = product.get("url_api_detail") or detail.get("url_api_detail")
    if not isinstance(link, str) or not link:
        return None
    try:
        url = httpx.URL(link)
    except httpx.InvalidURL:
        return None
    if url.scheme != "https" or url.host != "ekt.kz" or url.port not in (None, 443):
        return None
    return link


def check_available(product: dict) -> bool:
    """Есть ли учётные данные API и ссылка на карточку, чтобы запросить свежий остаток."""
    return credentials() is not None and detail_url(product) is not None


def live_quantity(product: dict, timeout: float = LIVE_TIMEOUT) -> int | None:
    """Актуальный остаток из API или None, если запрос невозможен или не удался.

    При успехе кэш в памяти получает свежие quantity и stores этого товара.
    """
    auth = credentials()
    link = detail_url(product)
    if auth is None or link is None:
        return None
    try:
        with httpx.Client(auth=httpx.BasicAuth(*auth), timeout=timeout) as client:
            response = client.get(link)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or str(payload.get("id")) != str(product.get("id")):
        return None
    quantity = payload.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity):
        return None
    fresh = max(int(quantity), 0)
    catalog.update_stock(product, fresh, payload.get("stores"))
    return fresh
