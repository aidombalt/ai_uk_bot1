"""Детектор рекламы / спама / запрещённого контента в чатах ЖК.

Принципы дизайна:
* Hard-coded whitelist домен.госов СПб и федеральных ресурсов — пересланная
  ссылка на закон / постановление НИКОГДА не должна стать причиной бана.
* Strong-сигналы (жаргон наркотиков) — спам сразу.
* Weak-сигналы (одно слово «крипта») — спам ТОЛЬКО в комбинации.
* Возвращает не bool, а структурированный SpamVerdict с категорией и доказательствами,
  чтобы управляющий в карточке эскалации понимал, ПОЧЕМУ бот так решил.

Whitelist домены подобраны вручную по результатам реальной верификации
адресов (см. поисковые запросы в истории разработки). Не использовать
LLM для расширения списка — добавление в whitelist должно быть осознанным
действием, иначе появятся дыры безопасности.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


# === WHITELIST: государственные и проверенные ресурсы ========================
# Если в сообщении есть ссылка на эти домены (или их поддомены) — это сильный
# сигнал, что пересылается легитимная информация (закон, новость комитета,
# постановление). Спам с whitelist-ссылкой почти невозможен.
_WHITELIST_DOMAINS: frozenset[str] = frozenset({
    # === Федеральные госресурсы ===
    "gosuslugi.ru",                # Госуслуги (вкл. dom.gosuslugi.ru)
    "pravo.gov.ru",                # официальный портал правовой информации
    "publication.pravo.gov.ru",
    "gov.ru",                      # *.gov.ru — общий поддомен правительства
    "kremlin.ru",                  # президент
    "council.gov.ru",              # Совет Федерации
    "duma.gov.ru",                 # Госдума
    "minstroyrf.gov.ru",           # Минстрой РФ (ЖКХ)
    "minfin.gov.ru",
    "nalog.gov.ru", "nalog.ru",    # ФНС
    "mvd.ru",                      # МВД
    "mchs.gov.ru",                 # МЧС
    "rospotrebnadzor.ru",          # Роспотребнадзор
    "fas.gov.ru",                  # ФАС (тарифы)
    "rkn.gov.ru",                  # Роскомнадзор
    "fssp.gov.ru",                 # ФССП
    "sudrf.ru",                    # суды РФ
    "vsrf.ru",                     # Верховный суд
    "ksrf.ru",                     # Конституционный суд

    # === СПб: администрация и комитеты ===
    "spb.ru",                      # *.spb.ru — общий
    "gov.spb.ru",                  # официальный сайт администрации СПб
    "gu.spb.ru",                   # государственные учреждения СПб
    "old.gu.spb.ru",
    "gilkom-complex.ru",           # Жилищный комитет
    "kgainfra.gov.spb.ru",         # Комитет благоустройства
    "kio.gov.spb.ru",              # Имущественные отношения
    "fincom.gov.spb.ru",           # Финансовый комитет
    "iss.gov.spb.ru",              # справочник органов власти
    "petersburg.ru",               # городской портал

    # === СПб: ресурсоснабжающие организации (РСО) ===
    "vodokanal.spb.ru",            # Водоканал СПб
    "tgc1.ru",                     # ТГК-1 (тепло)
    "rosseti-lenenergo.ru",        # Россети Ленэнерго (электросети)
    "lenenergo.ru",                # альтернативный домен
    "pesc.ru",                     # Петербургская сбытовая компания
    "pes.spb.ru",                  # Петроэлектросбыт

    # === Справочно-правовые системы (стандарт для ссылок на нормативку) ===
    "consultant.ru",               # КонсультантПлюс
    "garant.ru",                   # Гарант
    "kodeks.ru",                   # Кодекс
    "pravo.ru",                    # Право.ру (новости)

    # === Полезное для жильцов: банки/госжкх-партнёры (опционально) ===
    "rosreestr.gov.ru",            # Росреестр
    "fkr-spb.ru",                  # Фонд капремонта СПб
    "max.ru",                      # сам Max — внутренние ссылки

    # === Госуслуги и связанные ===
    "esia.gosuslugi.ru",
    "my.dom.gosuslugi.ru",
})


# === Регулярки извлечения ссылок и упоминаний ================================
_URL_RE = re.compile(
    r"https?://[^\s<>\"]+|(?<![@\w])[\w.-]+\.(?:ru|com|org|net|info|me|io|su|рф)(?:/[^\s]*)?",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"@\w{3,32}")
_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")


# === BLACKLIST: жаргон наркотиков (strong) ===================================
# Каждое — strong-сигнал. Хватает одного. Источник: общеизвестные интернет-сленг
# термины. Используем подстроки, чтобы ловить разные формы.
_DRUG_MARKERS: tuple[str, ...] = (
    # Опиаты, стимуляторы (общий сленг)
    "соли соль", "соль кристал",  # требуем уточняющий контекст для "соль"
    "мефедрон", "амфетамин", "метамфетамин", "метадон", "героин",
    "кокаин", " кокс ", "лсд", "lsd", "экстази", "ecstasy",
    "мдма", "mdma", "кетамин",
    # Каннабис
    "марихуан", " шишк", " бошк", " гашиш", "гашик",
    # Сленг "лавки/закладки"
    "закладк", " закладка", "клад готов", "забери клад",
    # Распространённый сленг в спам-каналах
    " мяу мяу", "скорост ", " спид ",  # с пробелами чтобы не ловить "скорость работы"
    " фен ", " шмаль", " дудк", " травк",
    # Сигналы "магазина" наркотиков
    "магазин закладок", "наш магаз", "телега @", "тор магазин", "tor магазин",
    # Грибы/психоделики
    "псилоциб", " грибы псилоц",
)

# === Криптовалюты / инвестиционный спам (weak — нужна комбинация) ===========
_CRYPTO_MARKERS: tuple[str, ...] = (
    "криптовалют", "криптобирж", "крипто-биржа", "биткоин", "btc/usdt",
    "ethereum", "ether ", "tether", "usdt",
    "трейдинг", "трейдер",
    "binance", "bybit", "okx", "huobi", "bingx", "kucoin",
    "p2p обмен", "арбитраж крипт", "сигналы трейдер",
    "доходност", "пассивный доход",
)

# === "Лёгкий заработок" / финансовая пирамида ================================
_EARN_MARKERS: tuple[str, ...] = (
    "лёгкий заработок", "легкий заработок", "лёгкие деньги", "легкие деньги",
    "от 50 000 в месяц", "от 100 000 в месяц", "от 200 000",
    "доход от 50", "доход от 100", "пассивный доход",
    "финансовая независимость", "финансовая свобода",
    "млм", "сетевой маркетинг",
    "приватная группа", "закрытый канал инвест",
    "обучение трейдингу", "научу зарабатывать",
)

# === Эзотерика / "услуги" =====================================================
_ESOTERIC_MARKERS: tuple[str, ...] = (
    "приворот", "отворот", "снять порчу", "порчу снять",
    "потомственный маг", "ясновидящ", "сильный маг",
    "верну любим", "любовная магия",
    "гадание онлайн", "таро онлайн",
)

# === Прямой коммерческий спам ================================================
_COMMERCE_MARKERS: tuple[str, ...] = (
    "оптом и в розницу", "доставка по россии", "доставка по спб дёшево",
    "промокод на скидку", "по промокоду скидка",
    "кредит без отказа", "займ без проверки", "займ онлайн",
    "ставки на спорт", "букмекер",
    "сдается посуточно", "посуточная аренда квартир",
    "продам квартиру срочно",  # реклама агентства
)

# === Подозрительные домены (анти-whitelist) =================================
# Домены явно не для ЖК-чата. Не блокируем сразу, но повышаем подозрение.
_SUSPICIOUS_DOMAINS: frozenset[str] = frozenset({
    "binance.com", "bybit.com", "okx.com", "huobi.com",
    "bingx.com", "kucoin.com", "kraken.com", "bitget.com",
    "1xbet.com", "1xbet.ru", "fonbet.ru",
    "stavka.com", "casino.com",
})


@dataclass
class SpamVerdict:
    is_spam: bool
    category: str | None = None  # "drugs" | "crypto" | "earn" | "ads" | "esoteric" | "mass_mention" | None
    confidence: float = 0.0      # 0..1
    matched: list[str] = field(default_factory=list)  # что сработало
    safe_links: list[str] = field(default_factory=list)  # ссылки на whitelist-домены (для логов)


def _extract_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;)") for m in _URL_RE.finditer(text)]


def _domain_of(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _is_whitelisted(domain: str) -> bool:
    """Проверяет, входит ли домен или его родитель в whitelist."""
    if not domain:
        return False
    parts = domain.split(".")
    # Проверяем домен и все его «родительские» вариации (foo.gov.spb.ru → gov.spb.ru → spb.ru)
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in _WHITELIST_DOMAINS:
            return True
    return False


def _count_matches(text_lc: str, markers: tuple[str, ...]) -> list[str]:
    return [m for m in markers if m in text_lc]


def detect(text: str) -> SpamVerdict:
    """Главный метод: анализирует текст и возвращает вердикт.

    Алгоритм (по порядку приоритета):
    1. Жаргон наркотиков → спам (drugs), даже одно совпадение.
    2. Извлекаем URL'ы. Если есть whitelist-ссылка — серьёзно повышаем планку
       (нужны strong-доказательства, чтобы это перебить).
    3. Криптовалютные/заработковые/эзо/коммерческие маркеры:
       - Один маркер — НЕ спам.
       - Два и больше — спам соответствующей категории.
    4. Подозрительная ссылка (binance.com и т.п.) + хотя бы 1 рекламный
       маркер → спам.
    5. Mass-mention (4+ упоминаний) + ссылка → спам.
    """
    if not text or len(text) < 3:
        return SpamVerdict(is_spam=False)

    text_lc = " " + text.lower() + " "  # рамки пробелов для подстрочного поиска

    # 1. Наркотики — strong-сигнал, бан без вопросов.
    drugs = _count_matches(text_lc, _DRUG_MARKERS)
    if drugs:
        return SpamVerdict(
            is_spam=True, category="drugs",
            confidence=0.95, matched=drugs,
        )

    # 2. Извлекаем URL'ы и оцениваем «доверенность».
    urls = _extract_urls(text)
    safe_urls: list[str] = []
    suspicious_urls: list[str] = []
    other_urls: list[str] = []
    for u in urls:
        domain = _domain_of(u)
        if _is_whitelisted(domain):
            safe_urls.append(u)
        elif domain in _SUSPICIOUS_DOMAINS:
            suspicious_urls.append(u)
        else:
            other_urls.append(u)

    # 3. Подсчёт маркеров категорий.
    crypto = _count_matches(text_lc, _CRYPTO_MARKERS)
    earn = _count_matches(text_lc, _EARN_MARKERS)
    esoteric = _count_matches(text_lc, _ESOTERIC_MARKERS)
    commerce = _count_matches(text_lc, _COMMERCE_MARKERS)

    # 4. Подозрительные ссылки + хотя бы 1 рекламный маркер → спам.
    if suspicious_urls and (crypto or earn):
        return SpamVerdict(
            is_spam=True, category="crypto",
            confidence=0.9,
            matched=suspicious_urls + crypto + earn,
        )

    # 5. Сильная защита для whitelist: даже если есть weak-маркеры, но ссылка
    # на gov-ресурс/закон — НЕ спам. Это пересылка инфы, не реклама.
    if safe_urls and not earn and not esoteric and not commerce:
        # whitelist-ссылка + одно слово «крипта» (например, в законе про крипту) — не спам
        return SpamVerdict(
            is_spam=False, safe_links=safe_urls,
            matched=crypto + earn + esoteric + commerce,
        )

    # 6. Криптовалюты: 2+ маркера или 1 маркер + ссылка не-whitelist.
    if len(crypto) >= 2 or (crypto and other_urls):
        return SpamVerdict(
            is_spam=True, category="crypto",
            confidence=0.8, matched=crypto,
            safe_links=safe_urls,
        )

    # 7. Заработок-спам: 1+ маркер уже подозрителен, 2+ — точно спам.
    if len(earn) >= 2 or (earn and (other_urls or _MENTION_RE.search(text))):
        return SpamVerdict(
            is_spam=True, category="earn",
            confidence=0.8, matched=earn,
            safe_links=safe_urls,
        )

    # 8. Эзотерика — почти всегда спам в ЖК-чате.
    if esoteric:
        return SpamVerdict(
            is_spam=True, category="esoteric",
            confidence=0.85, matched=esoteric,
            safe_links=safe_urls,
        )

    # 9. Коммерческий спам.
    if len(commerce) >= 2 or (commerce and (other_urls or _PHONE_RE.search(text))):
        return SpamVerdict(
            is_spam=True, category="ads",
            confidence=0.75, matched=commerce,
            safe_links=safe_urls,
        )

    # 10. Mass-mention + ссылка не-whitelist → попытка собрать аудиторию.
    mentions = _MENTION_RE.findall(text)
    if len(mentions) >= 4 and other_urls:
        return SpamVerdict(
            is_spam=True, category="mass_mention",
            confidence=0.7,
            matched=mentions[:5] + other_urls,
            safe_links=safe_urls,
        )

    return SpamVerdict(is_spam=False, safe_links=safe_urls)
