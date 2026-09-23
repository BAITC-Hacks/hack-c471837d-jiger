"""HTML-страница корзины: собирается на сервере при каждом запросе, все значения экранируются."""

import html
from urllib.parse import urlsplit

PAGE = """<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#132a47">
    <meta name="robots" content="noindex">
    <title>Корзина — ekt.kz</title>
    <link rel="icon" href="data:,">
    <style>
      :root {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #172b46;
        background: #edf1f6;
        color-scheme: light;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: start center;
        padding: 24px;
        background: radial-gradient(ellipse at top left, #dce8f4, transparent 60%), #edf1f6;
      }
      .cart {
        width: min(100%, 860px);
        overflow: hidden;
        background: #fff;
        border: 1px solid #dce3ec;
        border-radius: 24px;
        box-shadow: 0 20px 70px #132a4714;
      }
      .header {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 24px;
        color: #fff;
        background: #132a47;
      }
      .logo {
        display: grid;
        place-items: center;
        flex: 0 0 48px;
        height: 48px;
        border-radius: 14px;
        background: #ffcd45;
        color: #132a47;
        font-size: 30px;
        font-weight: 800;
      }
      h1 { margin: 0 0 5px; font-size: 22px; letter-spacing: -.4px; }
      .subtitle { margin: 0; color: #c7d5e5; font-size: 13px; line-height: 1.5; }
      .back {
        margin-left: auto;
        padding: 10px 14px;
        border-radius: 12px;
        background: #ffcd45;
        color: #172b46;
        font-size: 14px;
        font-weight: 700;
        text-decoration: none;
        white-space: nowrap;
      }
      .back:hover { background: #ffc321; }
      .content { padding: 24px; }
      .table-wrap { overflow-x: auto; }
      table { width: 100%; border-collapse: collapse; font-size: 14px; }
      th, td { padding: 12px 10px; border-bottom: 1px solid #e6ebf1; text-align: left; vertical-align: top; }
      th { color: #68788c; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
      td.num, th.num { text-align: right; white-space: nowrap; }
      .article { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; color: #3d4c60; }
      a { color: #1c4b7c; font-weight: 600; text-decoration: underline; text-underline-offset: 2px; }
      tfoot td { border-bottom: 0; font-weight: 700; font-size: 15px; }
      .empty { padding: 40px 0; color: #68788c; text-align: center; font-size: 15px; }
      .note { margin: 18px 0 0; color: #7b8898; font-size: 12px; }
      @media (max-width: 520px) {
        body { padding: 0; }
        .cart { border: 0; border-radius: 0; }
        .header { padding: 20px 16px; }
        .content { padding: 16px; }
        th, td { padding: 10px 6px; }
      }
    </style>
  </head>
  <body>
    <main class="cart" aria-labelledby="cart-title">
      <header class="header">
        <span class="logo" aria-hidden="true">ϟ</span>
        <div>
          <h1 id="cart-title">Корзина</h1>
          <p class="subtitle">__SUBTITLE__</p>
        </div>
        <a class="back" href="/">К ассистенту</a>
      </header>
      <section class="content">
__BODY__
        <p class="note">Страница показывает текущее состояние корзины на момент открытия. Чтобы обновить, перезагрузите её.</p>
      </section>
    </main>
  </body>
</html>
"""

EMPTY_BODY = '        <p class="empty">Корзина пуста. Попросите ассистента добавить товар и подтвердите добавление.</p>'


def format_money(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "цена не указана"
    if float(value).is_integer():
        text = f"{int(value):,}".replace(",", " ")
    else:
        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₸"


def safe_url(url: object) -> str | None:
    """Только абсолютные http(s)-ссылки попадают в href."""
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate
    return None


def _row(item: dict) -> str:
    name = html.escape(str(item.get("name") or item.get("article") or "Товар"))
    href = safe_url(item.get("url"))
    if href:
        name = f'<a href="{html.escape(href)}" target="_blank" rel="noopener noreferrer">{name}</a>'
    return (
        "          <tr>"
        f'<td class="article">{html.escape(str(item.get("article") or "—"))}</td>'
        f"<td>{name}</td>"
        f'<td class="num">{html.escape(str(item.get("qty")))} шт.</td>'
        f'<td class="num">{html.escape(format_money(item.get("price")))}</td>'
        f'<td class="num">{html.escape(format_money(item.get("total")))}</td>'
        "</tr>"
    )


def render_cart_page(state: dict) -> str:
    items = state.get("items") or []
    if not items:
        return PAGE.replace("__SUBTITLE__", "Пока пусто").replace("__BODY__", EMPTY_BODY)
    count = len(items)
    total_quantity = sum(int(item.get("qty") or 0) for item in items)
    subtitle = html.escape(f"Позиций: {count}, товаров: {total_quantity} шт.")
    rows = "\n".join(_row(item) for item in items)
    body = (
        '        <div class="table-wrap">\n'
        "        <table>\n"
        "          <thead><tr><th>Артикул</th><th>Название</th>"
        '<th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Сумма</th></tr></thead>\n'
        "          <tbody>\n"
        f"{rows}\n"
        "          </tbody>\n"
        f'          <tfoot><tr><td colspan="4">Итого</td><td class="num">'
        f'{html.escape(format_money(state.get("total")))}</td></tr></tfoot>\n'
        "        </table>\n"
        "        </div>"
    )
    return PAGE.replace("__SUBTITLE__", subtitle).replace("__BODY__", body)
