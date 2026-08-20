"""Deterministic sales-conversation guardrails for the academy Messenger bot."""
from __future__ import annotations

import re
from typing import Any

POLICY_MARKER = "__ACADEMY_SALES_CONVERSATION_POLICY_V1__"

# Explicit commitment signals. Informational questions deliberately do not match.
ENROLLMENT_PATTERNS = (
    r"\bبدي\s+(?:سجل|سجّل|احجز|احج|اثبت|ثبت)\b",
    r"\bبدي\s+التسجيل\b",
    r"\bبدي\s+احجز\b",
    r"\bاريد\s+(?:التسجيل|الحجز|احجز)\b",
    r"\bحاب[ةه]\s+اسجل\b",
    r"\bحاب[ةه]\s+احجز\b",
    r"\bثبتيلي\b",
    r"\bثبت(?:ي)?\s+(?:لي|لنا|المقعد|المكان|اسمي|التسجيل)\b",
    r"\bكيف\s+(?:ثبت|اثبت)\s+(?:مقعد|اسمي|التسجيل)\b",
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

BANNED_AFFECTION_TERMS = (
    "حبيبي",
    "حبيبتي",
    "غاليتي",
    "غالية",
    "غالي",
    "يا غالي",
    "يا غالية",
    "يا حبيب",
    "يا حبيبتي",
    "عيوني",
    "لعيونك",
    "تؤبريني",
)

REPLACEMENTS = {
    "يا غالي": "أهلاً بك",
    "يا غالية": "أهلاً بكِ",
    "يا حبيبتي": "أهلاً بكِ",
    "حبيبتي": "أهلاً بكِ",
    "حبيبي": "أهلاً بك",
    "غاليتي": "أهلاً بكِ",
    "غالية": "أهلاً بكِ",
    "غالي": "أهلاً بك",
    "يا حبيب": "أهلاً بك",
    "عيوني": "بكل احترام",
    "لعيونك": "بكل سرور",
    "تؤبريني": "أهلاً بكِ",
}


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
    """Allow payment details only on explicit enrollment or direct payment questions."""
    return has_enrollment_intent(user_message) or is_payment_question(user_message)


def sanitize_professional_response(text: str) -> str:
    value = (text or "").strip()
    for banned, replacement in REPLACEMENTS.items():
        value = value.replace(banned, replacement)
    return value


SALES_POLICY = r"""
=== سياسة المحادثة البيعية الإلزامية ===
1) لا تقترح الحجز أو تثبيت الاسم أو تثبيت المقعد أو الدفع أو شام كاش من تلقاء نفسك أثناء مرحلة الاستفسار المعلوماتي.
2) أسئلة الدوام، المواعيد، المحاور، الأسعار، الشهادة، مدة الدورة، الأدوات، العنوان أو المقارنة بين الدورات هي أسئلة معلوماتية فقط. أجب عنها مباشرة وباختصار، ولا تختمها بدعوة للحجز أو الدفع.
3) انتقل إلى مرحلة التسجيل فقط عندما يعبّر المستخدم بوضوح عن رغبة في التسجيل/الحجز/التثبيت، مثل: "بدي سجل"، "بدي أحجز"، "ثبتيلي"، "كيف ثبت مقعدي".
4) إذا سأل المستخدم مباشرة عن طرق الدفع أو شام كاش، أجب عن السؤال الذي طرحه دون افتراض أنه حسم قراره، ولا تضغط عليه لإتمام التسجيل.
5) عند الوصول لنية تسجيل صريحة، يمكن شرح خطوات التثبيت والدفع عبر شام كاش بشكل مهني ومحترم.
6) لا تستخدم أي ألفاظ تدليل أو مَيَانة مثل: حبيبي، حبيبتي، غالي، غالية، غاليتي، يا غالي، يا غالية، يا حبيب، عيوني، لعيونك، تؤبريني.
7) أسلوب الخطاب رسمي، محترم، هادئ، ودود ومهني، بدون ألقاب عاطفية أو مبالغة.
8) لا تخترع قرار شراء للمستخدم. لا تعتبر مجرد السؤال عن السعر أو الدوام موافقة على التسجيل.
""".strip()


def apply_to_app(app: Any) -> Any:
    current = getattr(app, "SYSTEM_INSTRUCTION", "")
    if POLICY_MARKER not in current:
        app.SYSTEM_INSTRUCTION = current + "\n\n" + POLICY_MARKER + "\n" + SALES_POLICY
    return app
