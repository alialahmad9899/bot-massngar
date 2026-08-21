"""Deterministic sales-conversation and human-handover guardrails."""
from __future__ import annotations

import re
from typing import Any

POLICY_MARKER = "__ACADEMY_SALES_CONVERSATION_POLICY_V2__"

ENROLLMENT_PATTERNS = (
    r"\bبدي\s+(?:سجل|سجّل|احجز|احج|اثبت|ثبت)\b",
    r"\bبدي\s+التسجيل\b",
    r"\bبدي\s+احجز\b",
    r"\bاريد\s+(?:التسجيل|الحجز|احجز)\b",
    r"\bحاب[ةه]\s+اسجل\b",
    r"\bحاب[ةه]\s+احجز\b",
    r"\bثبتيلي\b",
    r"\bثبت(?:ي)?\s+(?:لي|لنا|المقعد|مقعدي|المكان|اسمي|التسجيل)\b",
    r"\bكيف\s+(?:ثبت|اثبت)\s+(?:مقعد|مقعدي|اسمي|التسجيل)\b",
    r"\bبدي\s+ثبت\b",
    r"\bبدي\s+ثبّت\b",
    r"\bبدي\s+سجّل\b",
)

PAYMENT_QUESTION_PATTERNS = (
    r"\bشو\s+(?:طرق|طريقة)\s+الدفع\b",
    r"\bكيف\s+(?:الدفع|ادفع)\b",
    r"\bكيف\s+بقدر\s+ادفع\b",
    r"\bشام\s*كاش\b",
    r"\bطرق\s+التثبيت\b",
)

HANDOVER_PATTERNS = (
    r"\bبدي\s+(?:موظف|موظفة|شخص|حدا|انسان)\b",
    r"\bتواصل\s+(?:مع|مباشر)\b",
    r"\bاحكي\s+مع\s+(?:حدا|موظف|موظفة|الادارة|الإدارة)\b",
    r"\bبدي\s+اتواصل\s+مع\s+(?:الادارة|الإدارة)\b",
    r"\bخلي\s+(?:حدا|موظف|موظفة)\s+من\s+(?:الادارة|الإدارة)\b",
    r"\bبتواصل\s+مع\s+(?:الادارة|الإدارة)\b",
    r"\bتواصل\s+مع\s+(?:الادارة|الإدارة)\b",
    r"\bالإدارة\b",
    r"\bالادارة\b",
    r"\bموظف\b",
    r"\bموظفة\b",
    r"\bبشري\b",
    r"\bبشرية\b",
)

BANNED_AFFECTION_TERMS = (
    "حبيبي", "حبيبتي", "حبيب", "حبيبة", "غاليتي", "غالية", "غالي",
    "يا غالي", "يا غالية", "يا حبيب", "يا حبيبتي", "عيوني", "لعيونك", "تؤبريني",
    "بابا", "ماما", "بيبي", "قلبي", "يا قلبي", "روحي", "يا روحي", "عمري", "يا عمري",
    "حياتي", "يا حياتي", "عسل", "يا عسل", "قمر", "يا قمر", "ملكة", "يا ملكة",
    "جميلتي", "يا جميلتي", "يا جميلة", "يا جميل", "روح قلبي",
)

REPLACEMENTS = {
    "يا غالي": "أهلاً بك",
    "يا غالية": "أهلاً بكِ",
    "يا حبيبتي": "أهلاً بكِ",
    "حبيبتي": "أهلاً بكِ",
    "حبيب": "أهلاً بك",
    "حبيبي": "أهلاً بك",
    "غاليتي": "أهلاً بكِ",
    "غالية": "أهلاً بكِ",
    "غالي": "أهلاً بك",
    "يا حبيب": "أهلاً بك",
    "عيوني": "بكل احترام",
    "لعيونك": "بكل سرور",
    "تؤبريني": "أهلاً بكِ",
    "بابا": "أهلاً بك",
    "ماما": "أهلاً بكِ",
    "بيبي": "أهلاً بك",
    "قلبي": "أهلاً بك",
    "يا قلبي": "أهلاً بك",
    "روحي": "أهلاً بك",
    "يا روحي": "أهلاً بك",
    "عمري": "أهلاً بك",
    "يا عمري": "أهلاً بك",
    "حياتي": "أهلاً بكِ",
    "يا حياتي": "أهلاً بكِ",
    "عسل": "أهلاً بك",
    "يا عسل": "أهلاً بك",
    "قمر": "أهلاً بكِ",
    "يا قمر": "أهلاً بكِ",
    "ملكة": "أهلاً بكِ",
    "يا ملكة": "أهلاً بكِ",
    "جميلتي": "أهلاً بكِ",
    "يا جميلتي": "أهلاً بكِ",
    "يا جميلة": "أهلاً بكِ",
    "يا جميل": "أهلاً بك",
    "روح قلبي": "أهلاً بك",
}

PAYMENT_CTA_TERMS = (
    "شام كاش", "شامكاش", "ثبتّي", "ثبتي", "ثبتلي", "تثبيت", "تثبيت الاسم",
    "تثبيت المقعد", "تسجيل الاسم", "احجز", "حجز", "سجل", "تسجيل", "الدفعة الأولى",
    "الدفعة الاولى", "الدفع عن بعد", "ثبت مقعد",
)


def _normalize(text: str) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670]", "", value)
    for src, dst in {"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}.items():
        value = value.replace(src, dst)
    return re.sub(r"\s+", " ", value)


def has_enrollment_intent(user_message: str) -> bool:
    normalized = _normalize(user_message)
    return any(re.search(pattern, normalized) for pattern in ENROLLMENT_PATTERNS)


def is_payment_question(user_message: str) -> bool:
    normalized = _normalize(user_message)
    return any(re.search(pattern, normalized) for pattern in PAYMENT_QUESTION_PATTERNS)


def should_offer_payment(user_message: str) -> bool:
    return has_enrollment_intent(user_message) or is_payment_question(user_message)


def has_handover_intent(user_message: str) -> bool:
    normalized = _normalize(user_message)
    return any(re.search(pattern, normalized) for pattern in HANDOVER_PATTERNS)


def sanitize_professional_response(text: str) -> str:
    value = (text or "").strip()
    for banned in sorted(REPLACEMENTS, key=len, reverse=True):
        value = value.replace(banned, REPLACEMENTS[banned])
    return value


def guard_response(user_message: str, response_text: str) -> str:
    value = sanitize_professional_response(response_text)
    if should_offer_payment(user_message):
        return value

    parts = re.split(r"(?<=[.!؟\n])\s+", value)
    kept = []
    for part in parts:
        normalized = _normalize(part)
        if any(term in normalized for term in PAYMENT_CTA_TERMS):
            continue
        kept.append(part)
    cleaned = " ".join(p.strip() for p in kept if p.strip()).strip()
    return cleaned or "تفضلي، اذكري لي المعلومة التي تودين معرفتها وسأوضحها لكِ بشكل مباشر."


def is_human_page_echo(event: dict, bot_sent_mids: dict | None = None) -> bool:
    message = event.get("message") or {}
    if not message.get("is_echo"):
        return False
    mid = message.get("mid")
    return not bool(mid and bot_sent_mids and mid in bot_sent_mids)


SALES_POLICY = r"""
=== سياسة المحادثة البيعية والتواصل البشري الإلزامية ===
1) لا تقترح الحجز أو تثبيت الاسم أو تثبيت المقعد أو الدفع أو شام كاش من تلقاء نفسك أثناء مرحلة الاستفسار المعلوماتي.
2) أسئلة الدوام، المواعيد، المحاور، الأسعار، الشهادة، مدة الدورة، الأدوات، العنوان أو المقارنة بين الدورات هي أسئلة معلوماتية فقط. أجب عنها مباشرة، ولا تختمها بدعوة للحجز أو الدفع.
3) انتقل إلى مرحلة التسجيل فقط عندما يعبّر المستخدم بوضوح عن رغبة في التسجيل/الحجز/التثبيت.
4) إذا سأل المستخدم مباشرة عن طرق الدفع أو شام كاش، أجب عن السؤال الذي طرحه دون افتراض أنه حسم قراره.
5) عند الوصول لنية تسجيل صريحة، يمكن شرح خطوات التثبيت والدفع عبر شام كاش بشكل مهني ومحترم.
6) لا تستخدم أي ألفاظ تدليل أو مَيَانة مثل: حبيبي، حبيبتي، حبيب، غالي، غالية، غاليتي، بابا، ماما، بيبي، قلبي، روحي، عمري، حياتي، عسل، قمر، ملكة أو أي لقب عاطفي مشابه.
7) أسلوب الخطاب رسمي، محترم، هادئ، ودود ومهني، بدون ألقاب عاطفية أو مبالغة أو لغة شارع.
8) لا تخترع قرار شراء للمستخدم. لا تعتبر مجرد السؤال عن السعر أو الدوام موافقة على التسجيل.
9) إذا طلب المستخدم موظفاً أو الإدارة أو تواصلاً بشرياً، اعتبر المحادثة محوّلة لبشر: لا تكمل البيع ولا تجيب آلياً بعد التحويل، وتدع الموظف يتابع المحادثة.
""".strip()


def apply_to_app(app: Any) -> Any:
    current = getattr(app, "SYSTEM_INSTRUCTION", "")
    if POLICY_MARKER not in current:
        app.SYSTEM_INSTRUCTION = current + "\n\n" + POLICY_MARKER + "\n" + SALES_POLICY
    return app
