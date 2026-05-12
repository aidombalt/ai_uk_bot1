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


# === НОРМАЛИЗАЦИЯ ОБФУСЦИРОВАННОГО ТЕКСТА ====================================
# Спамеры заменяют кириллические буквы визуально похожими латинскими (а→a,
# с→c, е→e, о→o, р→p, х→x) и цифрами (б→6), разбивают слова пробелами
# («р а б о т ы»), прячут слова за эмодзи («🍁шишкu🍁»).
# Перед проверкой маркеров нормализуем текст в «чистую» кириллицу.

# Таблица: латинский гомоглиф / цифра → кириллица
_HOMOGLYPH_TABLE: dict[int, str] = {
    # строчные
    ord('a'): 'а', ord('c'): 'с', ord('e'): 'е',
    ord('o'): 'о', ord('p'): 'р', ord('x'): 'х',
    ord('y'): 'у', ord('u'): 'и',   # шишкu → шишки
    # прописные
    ord('A'): 'А', ord('B'): 'В', ord('C'): 'С',
    ord('E'): 'Е', ord('H'): 'Н', ord('K'): 'К',
    ord('M'): 'М', ord('O'): 'О', ord('P'): 'Р',
    ord('T'): 'Т', ord('X'): 'Х',
    # цифровые замены
    ord('6'): 'б',   # p a 6 o т ы → работы
}

# Диапазоны Unicode для emoji (покрывают ❄️ 🔥 📦 🍁 и большинство остальных)
_EMOJI_RE = re.compile(
    r"[\U00002600-\U000027BF"    # разные символы, кубики, стрелки
    r"\U00002B00-\U00002BFF"     # разные стрелки
    r"\U0001F000-\U0001F9FF"     # основные emoji-блоки
    r"\U0001FA00-\U0001FA9F"     # доп. emoji
    r"️⃣]+",           # variation selector, combining enclosing keycap
    re.UNICODE,
)

# Склейка разбитых слов: 4+ одиночных символов через пробел → слово.
# {3,} = «(пробел+символ)» повторяется ≥3 раза → всего ≥4 символа.
# Порог 4 защищает от склейки обычных однобуквенных слов «а б».
_SPACED_WORD_RE = re.compile(r'(?<!\S)\S(?: \S){3,}(?!\S)')


def _normalize_obfuscated(text: str) -> str:
    """Нормализует обфусцированный текст перед проверкой маркеров спама.

    1. Латинские гомоглифы → кириллица (а→a, с→c и т.д.)
    2. Emoji → пробел (🍁шишки🍁 → _шишки_)
    3. Разбитые пробелами буквы → слово (р а б о т ы → работы)
    """
    result = text.translate(_HOMOGLYPH_TABLE)
    result = _EMOJI_RE.sub(' ', result)
    result = _SPACED_WORD_RE.sub(lambda m: m.group(0).replace(' ', ''), result)
    return result


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
    "кристалл",  # слэнг мефедрона/первитина; «кристальный» пишется с ь — не совпадает
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
    # Типичные фразы объявлений о «курьерской» работе по доставке наркотиков
    "оплата каждый",      # «оплата каждый день» / «оплата каждые сутки»
    "ежедневная оплат",   # «ежедневная оплата»
    "доход от 15",        # «доход от 150к в неделю» (150 = 15+0) и выше
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


def _count_matches_either(
    text_lc: str, norm_lc: str, markers: tuple[str, ...]
) -> list[str]:
    """Ищет маркеры в оригинальном ИЛИ нормализованном тексте.

    Нормализованный текст ловит обфускацию (гомоглифы, пробелы в словах,
    emoji-границы); оригинальный — прямые совпадения. Объединяем результаты.
    """
    return list({*_count_matches(text_lc, markers), *_count_matches(norm_lc, markers)})


def detect(text: str) -> SpamVerdict:
    """Главный метод: анализирует текст и возвращает вердикт.

    Алгоритм (по порядку приоритета):
    1. Жаргон наркотиков → спам (drugs), даже одно совпадение.
       Проверка ведётся по оригинальному И нормализованному тексту —
       нормализация снимает обфускацию (латинские гомоглифы, emoji-границы,
       разбитые пробелами слова).
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

    text_lc = " " + text.lower() + " "          # оригинал в нижнем регистре
    norm_lc = " " + _normalize_obfuscated(text).lower() + " "  # нормализованный

    # 1. Наркотики — strong-сигнал, бан без вопросов.
    drugs = _count_matches_either(text_lc, norm_lc, _DRUG_MARKERS)
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

    # 3. Подсчёт маркеров категорий (оригинал + нормализованный).
    crypto = _count_matches_either(text_lc, norm_lc, _CRYPTO_MARKERS)
    earn = _count_matches_either(text_lc, norm_lc, _EARN_MARKERS)
    esoteric = _count_matches_either(text_lc, norm_lc, _ESOTERIC_MARKERS)
    commerce = _count_matches_either(text_lc, norm_lc, _COMMERCE_MARKERS)

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
