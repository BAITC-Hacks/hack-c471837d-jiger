import json
import math
import re
import threading
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "products.json"
PRODUCT_FIELDS = (
    "id", "name", "article", "price", "category", "image", "url", "url_api_detail",
    "description", "quantity", "properties", "characteristics",
)
SEARCH_TOKEN = re.compile(r"\w+(?:[-./]\w+)*")
MEASUREMENT = re.compile(r"(?<![\w./-])(\d+(?:[.,]\d+)?)\s*(к?вт|м?а|к?в|мм|см|м|лм|к|гц)\b")


def _search_text(text: str) -> str:
    # Catalog descriptions use both "30мА" and "30 мА". Do not normalize inside SKUs.
    return MEASUREMENT.sub(lambda match: match[1].replace(",", ".") + match[2], text.casefold())
# Служебные свойства Bitrix, бесполезные для консультации.
NOISY_PROPERTY_KEYS = frozenset({
    "CML2_TRAITS", "IMYAKARTINKI", "SLIDER_PHOTOS", "BRAND_PRIORITY", "POKAZYVAT_TSENY",
    "insta", "RECOMMEND", "CML2_TAXES", "NOVINKA", "SPETSPREDLOZHENIE",
})
STORES_NOTE = "Перечислены только склады с остатком больше нуля; на остальных складах остаток 0."
MAX_SEARCH_RESULTS = 20
# Разделы каталога ekt.kz по сегментам URL: /catalog/<раздел>/<подраздел>/<серия>/<товар>/.
CATEGORY_LABELS = {
    "nizkovoltnaya_apparatura": "Низковольтная аппаратура",
    "spets_predlozhenie": "Спецпредложения",
    "izdeliya_dlya_montazha_i_instrument": "Изделия для монтажа и инструмент",
    "novinki": "Новинки",
    "rozetki_vyklyuchateli_korobki": "Розетки, выключатели, коробки",
    "svetilniki_lampy": "Светильники и лампы",
    "arkhiv": "Архив",
    "korzina_elektrika": "Корзина электрика",
    "avtomatizatsiya": "Автоматизация",
    "videonablyudenie_skud_signalizatsiya": "Видеонаблюдение, СКУД, сигнализация",
    "rozetki_vyklyuchateli_makel_bylectrica_t_plast_lezard_schnieder_electric":
        "Розетки и выключатели Makel, Bylectrica, T-Plast, Lezard, Schneider Electric",
    "klemmniki": "Клеммники",
    "shchitovoe_oborudovanie_aktsiya_10": "Щитовое оборудование (акция)",
    "modulnye_avtomaticheskie_vyklyuchateli": "Модульные автоматические выключатели",
    "udliniteli_kolodki_setevye_filtry_katushki": "Удлинители, колодки, сетевые фильтры, катушки",
    "rele_schneider_electric": "Реле Schneider Electric",
    "mufty": "Муфты",
    "avtomaticheskie_vyklyuchateli": "Автоматические выключатели",
    "rasprodazha_dekraft": "Распродажа DEKraft",
    "silovye_avtomaticheskie_vyklyuchateli": "Силовые автоматические выключатели",
    "ustroystvo_differentsialnoy_zashchity": "Устройства дифференциальной защиты (УЗО, АВДТ)",
    "svetilniki_dlya_vnutrennego_osveshcheniya": "Светильники для внутреннего освещения",
    "svetilniki_dlya_ulichnogo_osveshcheniya": "Светильники для уличного освещения",
    "mufty_kvt_fortiflex_vvodnaya": "Муфты КВТ Fortiflex вводные",
    "novinka_mufty_erg_optima": "Муфты ERG Optima",
    "novinka_mufty_kvt": "Муфты КВТ",
    "novinka_klemmy_expert_unit": "Клеммы Expert Unit",
    "unit_cashback": "UNIT cashback",
    "silovye_razyemy": "Силовые разъёмы",
    "udliniteli_shnury": "Удлинители и шнуры",
    "korobki": "Коробки",
    "kolodki": "Колодки",
    "klemmy_gilzy_nakonechniki": "Клеммы, гильзы, наконечники",
    "aktsiya_megalight_polnyy_sklad_polnyy_bak": "Акция MEGALIGHT",
    "differentsialnye_ustroystva_uzo_avdt_vd": "Дифференциальные устройства (УЗО, АВДТ, ВД)",
    "ustroystva_plavnogo_puska_iek_": "Устройства плавного пуска IEK",
    "lampy": "Лампы",
    "it_oborudovanie": "IT-оборудование",
}
CATEGORY_URL_RE = re.compile(r"ekt\.kz/catalog/(.+?)/[^/]+/?$")


def category_path(product: dict) -> list[str]:
    """Читаемые сегменты раздела каталога из url товара (без сегмента самого товара)."""
    url = product.get("url") or _detail(product).get("url") or ""
    match = CATEGORY_URL_RE.search(str(url))
    if not match:
        return []
    return [
        CATEGORY_LABELS.get(segment, segment.replace("_", " ").strip())
        for segment in match.group(1).split("/")
        if segment
    ]


def category_of(product: dict) -> str:
    return " / ".join(category_path(product))


def _detail(product: dict) -> dict:
    detail = product.get("detail")
    return detail if isinstance(detail, dict) else {}


def stores_in_stock(product: dict) -> list[dict]:
    """Склады с положительным остатком: [{name, quantity}]."""
    stores = _detail(product).get("stores")
    if stores is None:
        stores = product.get("stores")
    result = []
    for store in stores or []:
        if not isinstance(store, dict):
            continue
        quantity = store.get("quantity")
        if isinstance(quantity, (int, float)) and not isinstance(quantity, bool) and quantity > 0:
            result.append({"name": store.get("name"), "quantity": quantity})
    return result


def product_fields(product: dict) -> dict:
    """Краткая запись товара для результатов поиска: поля списка + остаток по складам."""
    detail = _detail(product)
    # Поля списка остаются исходными; отсутствующие берём из полной карточки.
    fields = {
        field: product[field] if field in product else detail[field]
        for field in PRODUCT_FIELDS
        if field in product or field in detail
    }
    quantity = _quantity(product)
    if "quantity" in product or "quantity" in detail:
        fields["quantity"] = quantity
    fields["in_stock"] = _in_stock(quantity)
    fields["stores"] = stores_in_stock(product)
    if not fields.get("category"):
        category = category_of(product)
        if category:
            fields["category"] = category
    return fields


def _properties(product: dict) -> dict:
    properties = _detail(product).get("properties") or product.get("properties")
    return properties if isinstance(properties, dict) else {}


def _quantity(product: dict) -> object:
    return _detail(product).get("quantity", product.get("quantity"))


def _in_stock(quantity: object) -> bool:
    return type(quantity) in (int, float) and math.isfinite(quantity) and quantity > 0


def stock_quantity(product: dict) -> int:
    """Общий остаток товара целым числом; неизвестный или отрицательный остаток — 0."""
    quantity = product_fields(product).get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        return 0
    return max(int(quantity), 0)


def min_batch(product: dict) -> int:
    """Кратность продажи из KRATNOST_MIN (в каталоге это строка); без ограничения — 1."""
    raw = _properties(product).get("KRATNOST_MIN")
    try:
        batch = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return batch if batch > 1 else 1


_CERTIFICATE_LABEL = re.compile(r"сертиф|certificat|sertifikat|(?:^|[_/\-])cert(?:[_/\-]|$)", re.IGNORECASE)
_DOCUMENT_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class _CertificateAnchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.href = ""
        self.label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            attributes = dict(attrs)
            self.href = attributes.get("href") or ""
            self.label = attributes.get("title") or ""

    def handle_data(self, data: str) -> None:
        if self.href:
            self.label += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            if _CERTIFICATE_LABEL.search(self.label):
                self.links.append(self.href)
            self.href = self.label = ""


def certificate_urls(product: dict) -> list[str]:
    """Expose only certificate links present in the catalog; never construct URLs.

    A PDF alone may be a manual. A certificate label in its property, text or
    anchor (or in the URL itself) is required. Links are not fetched here.
    """
    candidates: list[str] = []

    def collect(value: object, labeled: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                collect(child, labeled or bool(_CERTIFICATE_LABEL.search(str(key))))
        elif isinstance(value, list):
            for child in value:
                collect(child, labeled)
        elif isinstance(value, str):
            text = unescape(value)
            parser = _CertificateAnchors()
            parser.feed(text)
            candidates.extend(parser.links)
            for match in _DOCUMENT_URL.finditer(text):
                url = match.group().rstrip(".,;:)]}")
                if labeled or _CERTIFICATE_LABEL.search(url):
                    candidates.append(url)
                    continue
                # Plain text: the certificate label must immediately introduce
                # this link, rather than another document earlier in a paragraph.
                prefix = text[max(0, match.start() - 100):match.start()]
                if re.search(r"(?:сертификат\w*|certificate\w*|sertifikat\w*)[^<>\n:.;/]{0,70}:?\s*$", prefix, re.IGNORECASE):
                    candidates.append(url)

    for source in (product, _detail(product)):
        collect(source.get("description"))
        collect(source.get("properties"))
    links = []
    for url in candidates:
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and not parsed.password:
            if url not in links:
                links.append(url)
    return links


def product_card(product: dict) -> dict:
    """Полная карточка для get_product: описание, характеристики, склады, кратность."""
    detail = _detail(product)
    card = product_fields(product)
    card["description"] = detail.get("description") or product.get("description")
    properties = detail.get("properties") or product.get("properties")
    if isinstance(properties, dict):
        card["properties"] = {
            key: value for key, value in properties.items() if key not in NOISY_PROPERTY_KEYS
        }
        card["min_batch"] = properties.get("KRATNOST_MIN")
    card["stores_note"] = STORES_NOTE
    card["certificate_urls"] = certificate_urls(product)
    return card


def find_product(product_id: object) -> dict | None:
    """Товар каталога по id за O(1) или None."""
    return _catalog()["by_id"].get(str(product_id))


ANALOG_GROUP_KEYS = ("KATEGORIYA", "KATEGORIYA_SVETILNIKA", "TIP_USTROYSTVA")
ANALOG_PROPERTY_KEYS = (
    "NOMINALNYY_TOK", "KOLICHESTVO_POLYUSOV", "NOMINALNOE_NAPRYAZHENIE",
    "NOMINALNAYA_OTKLYUCHAYUSHCHAYA_SPOSOBNOST", "KHARAKTERISTIKA_SRABATYVANIYA",
    "TIP_USTANOVKI", "PLOSHCHAD_POPERECHNOGO_SECHENIYA", "KOLICHESTVO_ZHIL", "TSVET",
)
ANALOG_NAME_TOKEN = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:квт|вт|w|лм|lm|к|k|ма|ma|а|a|в|v|мм|mm)\b"
    r"|ip\s*\d+|\w+(?:[-./+]\w+)*", re.IGNORECASE,
)
ANALOG_STOP_WORDS = {"для", "под", "над", "на", "и", "с", "со", "в", "без", "шт", "мм", "см", "м", "из", "тип", "х-ка", "тестовый"}


def _comparison_value(value: object) -> str:
    text = " ".join(str(value).casefold().split()) if value is not None else ""
    # «63 А» и «63А», «4,5кА» и «4.5 кА» сравниваются одинаково; raw не меняем.
    text = re.sub(r"(?<=\d)\s+(?=[a-zа-я])", "", text)
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def _analog_url_group(product: dict) -> str:
    url = product.get("url") or _detail(product).get("url") or ""
    try:
        path = unquote(urlsplit(str(url)).path).strip("/").split("/")
    except ValueError:
        return ""
    if "catalog" not in path:
        return ""
    categories = path[path.index("catalog") + 1:-1]
    # Последний раздел перед slug товара точнее общих «акций»/«новинок».
    return categories[-1].casefold() if categories else ""


def _analog_name_tokens(product: dict) -> dict[str, str]:
    tokens = {}
    name = product.get("name") or _detail(product).get("name") or ""
    units = {"w": "вт", "lm": "лм", "k": "к", "ma": "ма", "a": "а", "v": "в", "mm": "мм"}
    for match in ANALOG_NAME_TOKEN.finditer(str(name)):
        raw = match.group()
        token = re.sub(r"\s+", "", raw.casefold()).replace(",", ".")
        token = re.sub(r"(?<=\d)(w|lm|k|ma|a|v|mm)$", lambda m: units[m[1]], token)
        tokens.setdefault(token, raw)
    return tokens


def find_analogs(product_id: int, max_results: int = 3) -> list[dict]:
    """Доступные товары близкой группы с объяснимыми совпадениями и отличиями."""
    if type(product_id) is not int or product_id < 1:
        raise ValueError("product_id должен быть положительным целым числом.")
    if type(max_results) is not int or not 1 <= max_results <= MAX_SEARCH_RESULTS:
        raise ValueError(f"max_results должен быть целым числом от 1 до {MAX_SEARCH_RESULTS}.")
    source = find_product(product_id)
    if source is None:
        return []
    source_props = _properties(source)
    source_group = _analog_url_group(source)
    source_tokens = _analog_name_tokens(source)
    source_specs = {token for token in source_tokens if (
        any(char.isdigit() for char in token) and any(char.isalpha() for char in token)
    )}
    candidates = []
    for product in load_catalog():
        quantity = _quantity(product)
        if str(product.get("id")) == str(product_id) or not _in_stock(quantity):
            continue
        if type(product.get("id")) not in (int, str) or not str(product["id"]).strip():
            continue
        props = _properties(product)
        common_groups = [key for key in ANALOG_GROUP_KEYS if (
            _comparison_value(source_props.get(key)) and _comparison_value(props.get(key))
        )]
        if any(_comparison_value(source_props[key]) != _comparison_value(props[key]) for key in common_groups):
            continue
        same_url_group = bool(source_group and source_group == _analog_url_group(product))
        if not common_groups and not same_url_group:
            continue

        matched = {}
        differs = {}
        for key in (*ANALOG_GROUP_KEYS, *ANALOG_PROPERTY_KEYS):
            original, candidate = source_props.get(key), props.get(key)
            if not (_comparison_value(original) or _comparison_value(candidate)):
                continue
            if _comparison_value(original) == _comparison_value(candidate):
                matched[key] = candidate
            else:
                differs[key] = {"source": original, "candidate": candidate}
        if same_url_group:
            matched["catalog_group"] = source_group
        brand_keys = ("TORGOVAYA_MARKA", "BRAND", "BREND")
        differs["brand"] = {
            "source": next((source_props[key] for key in brand_keys if source_props.get(key)), None),
            "candidate": next((props[key] for key in brand_keys if props.get(key)), None),
        }
        tokens = _analog_name_tokens(product)
        shared = source_tokens.keys() & tokens.keys()
        matched["name_tokens"] = [tokens[token] for token in sorted(shared)]
        differs["name_tokens"] = {
            "source": [source_tokens[token] for token in sorted(source_tokens.keys() - tokens.keys())],
            "candidate": [tokens[token] for token in sorted(tokens.keys() - source_tokens.keys())],
        }
        specs = {token for token in tokens if (
            any(char.isdigit() for char in token) and any(char.isalpha() for char in token)
        )}
        property_matches = sum(key in matched for key in ANALOG_PROPERTY_KEYS)
        property_differences = sum(key in differs for key in ANALOG_PROPERTY_KEYS)
        brand_tokens = _analog_name_tokens({"name": " ".join(
            str(value or "") for value in differs["brand"].values()
        )})
        meaningful_shared = {
            token for token in shared - brand_tokens.keys() - ANALOG_STOP_WORDS
            if len(token) > 1 and any(char.isalpha() for char in token)
        }
        # Общая распродажа и бренд сами по себе не делают лампу аналогом выключателя.
        if not property_matches and not meaningful_shared:
            continue
        score = (
            4 * property_matches - 2 * property_differences
            + 3 * len(source_specs & specs) - len(source_specs ^ specs)
            + len(shared) / max(len(source_tokens.keys() | tokens.keys()), 1)
        )
        fields = product_fields(product)
        item = {key: fields[key] for key in (
            "id", "name", "article", "price", "category", "url", "image", "url_api_detail",
        ) if key in fields}
        item.update(quantity=quantity, in_stock=True, matched=matched, differs=differs)
        candidates.append((score, item))

    candidates.sort(key=lambda entry: (-entry[0], str(entry[1].get("article", "")), str(entry[1]["id"])))
    results = []
    seen_ids = {str(product_id)}
    for _, item in candidates:
        if str(item["id"]) in seen_ids:
            continue
        seen_ids.add(str(item["id"]))
        results.append(item)
        if len(results) == max_results:
            break
    return results


def find_by_article(article: object) -> dict | None:
    """Товар по точному артикулу или псевдониму (регистр и внешние пробелы не важны) за O(1)."""
    if not isinstance(article, str):
        return None
    folded = article.casefold().strip()
    matches = _catalog()["by_article"].get(folded)
    if not matches:
        return None
    # Псевдоним может быть общим для нескольких товаров: предпочитаем основной артикул.
    for product in matches:
        if str(product.get("article") or "").casefold().strip() == folded:
            return product
    return matches[0]


def products_by_article(aliases: set[str]) -> set[int]:
    """Идентификаторы объектов товаров, у которых есть любой из псевдонимов артикула."""
    by_article = _catalog()["by_article"]
    return {id(product) for alias in aliases for product in by_article.get(alias, ())}


def update_stock(product: dict, quantity: int, stores: object = None) -> None:
    """Обновляет остаток товара в кэше памяти свежими данными API (до следующей перезагрузки)."""
    target = product["detail"] if isinstance(product.get("detail"), dict) else product
    with _cache_lock:
        target["quantity"] = quantity
        if isinstance(stores, list):
            target["stores"] = stores


def product_articles(product: dict) -> set[str]:
    """Read article aliases without dropping leading zeroes or separators."""
    articles = set()
    for source in (product, product.get("detail", {})):
        if not isinstance(source, dict):
            continue
        values = [source.get("article")]
        properties = source.get("properties")
        if isinstance(properties, dict):
            values.extend(properties.get(key) for key in ("CML2_ARTICLE", "ARTIKULPOSTAVSHCHIKA"))
        articles.update(str(value).casefold().strip() for value in values if value is not None)
    return articles - {""}


_cache_lock = threading.RLock()
# Кэш каталога в памяти: {"key": отпечаток файла, "products": [...], "by_id": {...}, "by_article": {...}}.
_cache: dict | None = None


def _read_catalog() -> list[dict]:
    """Читает и проверяет data/products.json с диска."""
    try:
        with CATALOG_PATH.open(encoding="utf-8") as file:
            products = json.load(file)
    except FileNotFoundError:
        raise RuntimeError(
            "Каталог data/products.json не найден: запустите python fetch_catalog.py "
            "(с uv: uv run python fetch_catalog.py)."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError(
            "Каталог data/products.json повреждён. Повторите uv run python fetch_catalog.py."
        ) from None
    except OSError:
        raise RuntimeError(
            "Не удалось прочитать data/products.json. Проверьте права доступа."
        ) from None

    if not isinstance(products, list) or any(
        not isinstance(product, dict) for product in products
    ):
        raise RuntimeError("Неверный формат каталога: ожидается список товаров.")
    return products


def _catalog_key() -> tuple:
    """Отпечаток файла каталога: путь, inode, время изменения и размер."""
    try:
        stat = CATALOG_PATH.stat()
    except FileNotFoundError:
        raise RuntimeError(
            "Каталог data/products.json не найден: запустите python fetch_catalog.py "
            "(с uv: uv run python fetch_catalog.py)."
        ) from None
    except OSError:
        raise RuntimeError(
            "Не удалось прочитать data/products.json. Проверьте права доступа."
        ) from None
    return (str(CATALOG_PATH), stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _build_index(products: list[dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    by_id: dict[str, dict] = {}
    by_article: dict[str, list[dict]] = {}
    for product in products:
        if product.get("id") is not None:
            by_id.setdefault(str(product["id"]), product)
        for alias in product_articles(product):
            by_article.setdefault(alias, []).append(product)
    return by_id, by_article


def _catalog() -> dict:
    """Каталог из памяти; с диска читается при старте и когда файл изменился."""
    global _cache
    key = _catalog_key()
    with _cache_lock:
        if _cache is None or _cache["key"] != key:
            products = _read_catalog()
            by_id, by_article = _build_index(products)
            _cache = {"key": key, "products": products, "by_id": by_id, "by_article": by_article}
        return _cache


def load_catalog() -> list[dict]:
    """Список товаров из кэша в памяти. Список общий для всех запросов: не изменять."""
    return _catalog()["products"]


def reload_catalog() -> list[dict]:
    """Принудительно перечитывает data/products.json и перестраивает индексы."""
    global _cache
    with _cache_lock:
        _cache = None
        return load_catalog()


def _price(product: dict) -> float | None:
    price = product.get("price", _detail(product).get("price"))
    if isinstance(price, str):
        try:
            price = float(price.replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return price


def search_products(
    query: str,
    max_results: int = 10,
    *,
    in_stock_only: bool = False,
    min_price: float | None = None,
    max_price: float | None = None,
    category: str | None = None,
    offset: int = 0,
) -> dict:
    """Поиск по словам с фильтрами. Возвращает {"total", "offset", "items"}.

    Пустой query допустим, если задан хотя бы один фильтр: тогда подбираются все
    товары каталога, подходящие под фильтры. Товары в наличии ранжируются выше,
    при равенстве — дешевле раньше.
    """
    if not isinstance(query, str):
        raise ValueError("Поисковый запрос должен быть строкой.")
    if not isinstance(in_stock_only, bool):
        raise ValueError("in_stock_only должен быть true или false.")
    if type(max_results) is not int or not 1 <= max_results <= MAX_SEARCH_RESULTS:
        raise ValueError(f"Количество результатов должно быть целым числом от 1 до {MAX_SEARCH_RESULTS}.")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset должен быть целым числом не меньше нуля.")
    for label, value in (("min_price", min_price), ("max_price", max_price)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise ValueError(f"{label} должен быть неотрицательным числом или null.")
    if category is not None and not isinstance(category, str):
        raise ValueError("category должна быть строкой или null.")

    folded_query = query.casefold().strip()
    # Keep whole identifiers: splitting TEST-LED-30-A-MISSING would match TEST-LED-30-A.
    article_words = set(SEARCH_TOKEN.findall(folded_query))
    words = set(SEARCH_TOKEN.findall(_search_text(folded_query)))
    identifiers = {word for word in words if not word.isalpha()}
    folded_category = (category or "").casefold().strip()
    has_filters = in_stock_only or min_price is not None or max_price is not None or bool(folded_category)
    if not words and not has_filters:
        raise ValueError("Укажите ключевые слова или хотя бы один фильтр: цена, наличие, категория.")

    # Точный артикул ищется по индексу: целый запрос или любой его токен-идентификатор.
    exact_products = products_by_article({folded_query, *article_words})
    matches = []
    for product in load_catalog():
        detail = _detail(product)
        sources = (product, detail)
        quantity = _quantity(product)
        in_stock = _in_stock(quantity)
        if in_stock_only and not in_stock:
            continue
        price = _price(product)
        if min_price is not None and (price is None or price < min_price):
            continue
        if max_price is not None and (price is None or price > max_price):
            continue
        category_text = category_of(product)
        if folded_category and folded_category not in category_text.casefold():
            continue

        searchable = " ".join(
            [category_text]
            + [
                str(source.get(field) or "")
                for source in sources
                for field in ("article", "name", "category", "description", "characteristics", "properties")
            ]
        ).casefold()
        searchable_words = set(SEARCH_TOKEN.findall(searchable))
        searchable = _search_text(searchable)
        searchable_words.update(SEARCH_TOKEN.findall(searchable))
        exact_article = id(product) in exact_products
        # Unknown codes must not fall back to a coincidentally matching name/category.
        # Whole numeric/specification tokens still allow queries such as "IP20" or "30 Вт".
        # An explicit known article still identifies the product when the buyer adds
        # an order quantity or a requested specification that needs checking.
        if not exact_article and not identifiers.issubset(searchable_words):
            continue
        score = sum(word in searchable for word in words)
        if words and not (exact_article or score):
            continue
        matches.append((exact_article, in_stock, score, price, product))

    # Точный артикул, затем наличие, затем совпадения; при равенстве — дешевле раньше.
    matches.sort(key=lambda match: (
        not match[0], not match[1], -match[2], match[3] if match[3] is not None else float("inf"),
    ))
    page = matches[offset:offset + max_results]
    return {
        "total": len(matches),
        "offset": offset,
        "items": [product_fields(product) for *_, product in page],
    }


def list_categories() -> list[dict]:
    """Разделы каталога (два уровня) с количеством товаров и товаров в наличии."""
    counts: dict[str, list[int]] = {}
    for product in load_catalog():
        path = category_path(product)
        if not path:
            continue
        key = " / ".join(path[:2])
        entry = counts.setdefault(key, [0, 0])
        entry[0] += 1
        quantity = product.get("quantity", _detail(product).get("quantity"))
        if isinstance(quantity, (int, float)) and quantity > 0:
            entry[1] += 1
    return [
        {"category": key, "total": total, "in_stock": in_stock}
        for key, (total, in_stock) in sorted(
            counts.items(), key=lambda item: (-item[1][1], -item[1][0], item[0])
        )
    ]
