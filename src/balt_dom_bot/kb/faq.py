"""База знаний: тематические шаблоны ответов в стиле УК (ТЗ §3.1, §9.4).

Используется как fallback и способ экономии токенов LLM на типовых ситуациях.
Шаблоны нейтральные: не упоминают название УК, без «мы/нам», на «Вы» с заглавной.

Каждый шаблон — `theme + ключевые слова + текст`. Совпадение требует и темы,
и хотя бы одного ключевого слова, чтобы избежать ложных срабатываний.
"""

from __future__ import annotations

from dataclasses import dataclass

from balt_dom_bot.models import Theme


@dataclass(frozen=True)
class FaqTemplate:
    theme: Theme
    keywords: tuple[str, ...]
    template: str  # поддерживает {greeting}, {complex_name}, {address}


FAQ: tuple[FaqTemplate, ...] = (
    # --- EMERGENCY ---
    FaqTemplate(
        theme=Theme.EMERGENCY,
        keywords=("потоп", "затопил", "затопле", "затопл", "прорыв"),
        template=(
            "{greeting}. Информация о затоплении принята, аварийная служба "
            "уведомлена и направляется на адрес. До прибытия специалистов "
            "по возможности перекройте подачу воды."
        ),
    ),
    FaqTemplate(
        theme=Theme.EMERGENCY,
        keywords=("горит", "пожар", "дым"),
        template=(
            "{greeting}. При признаках возгорания немедленно звоните 101 или 112. "
            "Информация передана аварийной службе, контроль ситуации обеспечен."
        ),
    ),
    # --- TECH_FAULT ---
    FaqTemplate(
        theme=Theme.TECH_FAULT,
        keywords=("лифт",),
        template=(
            "{greeting}, спасибо за обращение. Заявка на проверку лифтового "
            "оборудования принята и передана специалистам УК."
        ),
    ),
    FaqTemplate(
        theme=Theme.TECH_FAULT,
        keywords=("домофон",),
        template=(
            "{greeting}, замечание принято. Информация о неполадке домофонной "
            "системы передана специалистам УК."
        ),
    ),
    FaqTemplate(
        theme=Theme.TECH_FAULT,
        keywords=("сигнализаци",),
        template=(
            "{greeting}, информация о срабатывании сигнализации принята и "
            "передана специалистам УК для проверки."
        ),
    ),
    FaqTemplate(
        theme=Theme.TECH_FAULT,
        keywords=("шлагбаум", "ворота"),
        template=(
            "{greeting}, заявка на проверку работы шлагбаума принята и передана "
            "специалистам УК."
        ),
    ),
    # --- IMPROVEMENT ---
    FaqTemplate(
        theme=Theme.IMPROVEMENT,
        keywords=("уборк", "грязно", "мусор"),
        template=(
            "{greeting}, спасибо за обращение. Замечание по содержанию "
            "территории принято и передано специалистам УК для рассмотрения."
        ),
    ),
    FaqTemplate(
        theme=Theme.IMPROVEMENT,
        keywords=("детск", "площадк"),
        template=(
            "{greeting}, обращение по состоянию площадки принято и передано "
            "специалистам УК для рассмотрения."
        ),
    ),
    FaqTemplate(
        theme=Theme.IMPROVEMENT,
        keywords=("газон", "озеленен", "дерев"),
        template=(
            "{greeting}, спасибо за обращение. Вопросы благоустройства "
            "территории {complex_name} рассматриваются в плановом порядке "
            "совместно со специалистами."
        ),
    ),
    # --- SECURITY ---
    FaqTemplate(
        theme=Theme.SECURITY,
        keywords=("посторон", "чужая машина", "чужой автомобиль", "чужой авт"),
        template=(
            "{greeting}, спасибо за сигнал. Информация о постороннем "
            "транспорте принята и передана специалистам УК."
        ),
    ),
    FaqTemplate(
        theme=Theme.SECURITY,
        keywords=("охран", "пост"),
        template=(
            "{greeting}, обращение по работе охраны принято и передано "
            "специалистам УК для рассмотрения."
        ),
    ),
    # --- INFO_REQUEST ---
    FaqTemplate(
        theme=Theme.INFO_REQUEST,
        keywords=("режим", "график", "часы"),
        template=(
            "{greeting}. Актуальный режим работы и контактные телефоны "
            "опубликованы в закреплённом сообщении чата {complex_name}."
        ),
    ),
    FaqTemplate(
        theme=Theme.INFO_REQUEST,
        keywords=("телефон", "контакт"),
        template=(
            "{greeting}. Контактные данные специалистов размещены в "
            "закреплённом сообщении чата {complex_name}."
        ),
    ),
    # --- UTILITY ---
    FaqTemplate(
        theme=Theme.UTILITY,
        keywords=("горяч", "холодн", "вода", "отключ"),
        template=(
            "{greeting}, спасибо за обращение. Информация об отключении воды "
            "принята специалистами УК. Подробности по срокам будут "
            "опубликованы в чате при поступлении."
        ),
    ),
    FaqTemplate(
        theme=Theme.UTILITY,
        keywords=("отоплен", "тепло", "холодн в квартир", "батаре"),
        template=(
            "{greeting}, обращение по теплоснабжению принято и передано "
            "специалистам УК для проверки."
        ),
    ),
    FaqTemplate(
        theme=Theme.UTILITY,
        keywords=("электр", "свет", "напряжен"),
        template=(
            "{greeting}, замечание по электроснабжению принято и передано "
            "специалистам УК. Информация о сроках восстановления будет "
            "опубликована в чате при её поступлении."
        ),
    ),
    # LEGAL_ORG: FAQ нет намеренно — pipeline эскалирует такие темы
    # управляющему через `always_escalate_themes` (ТЗ §7.1).
)


def find_template(theme: Theme, text: str) -> FaqTemplate | None:
    """Совпадение по теме И ключевым словам — иначе None."""
    text_lc = text.lower()
    for tpl in FAQ:
        if tpl.theme != theme:
            continue
        if any(kw in text_lc for kw in tpl.keywords):
            return tpl
    return None


def format_template(tpl: FaqTemplate, *, name: str | None, complex_name: str, address: str) -> str:
    greeting = f"{name}, здравствуйте" if name else "Здравствуйте"
    return tpl.template.format(greeting=greeting, complex_name=complex_name, address=address)
