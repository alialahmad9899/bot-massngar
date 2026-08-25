"""Compatibility shim for the unified Syrian language policy."""
from __future__ import annotations

import re
import production_runtime_v2 as runtime

LANGUAGE_POLICY_MARKER = "__UNIFIED_SYRIAN_LANGUAGE_POLICY_V2__"
PREFERRED_FORMS = (
    ("ماذا", "شو"), ("لماذا", "ليش"), ("بهذا الشكل", "هيك"), ("لأنه", "لانو"),
    ("لا يوجد", "ما في"), ("يمكنك", "فيكي"), ("تستطيعين", "بتقدري"), ("أريد", "بدي"),
    ("الآن", "هلق"), ("هنا", "هون"), ("أيضاً", "كمان"), ("لذلك", "لهيك"),
)
BANNED_TERMS = set(runtime.BANNED) | {"أستاذ", "أستاذة", "استاذ", "استاذة", "آنسة", "انسة", "مدام"}
SYRIAN_LANGUAGE_GUIDE = runtime.SYRIAN_GUIDE
SHORT_SOCIAL_REPLIES = dict(runtime.SOCIAL)


def normalize_text(text: str) -> str:
    return runtime.normalize(text)


def social_reply(user_message: str) -> str | None:
    return runtime.social(user_message)


def sanitize_response(text: str) -> str:
    value = runtime.sanitize(text)
    for title in ("أستاذة", "أستاذ", "مدام", "آنسة", "استاذة", "استاذ", "انسة"):
        value = re.sub(rf"(?<!\w){re.escape(title)}(?!\w)", "", value)
    value = value.replace("إن شاء الله", "").replace("ان شاء الله", "")
    return re.sub(r"\s+", " ", value).strip(" .،")


def sanitize_history(history):
    cleaned = []
    for item in history or []:
        role = item.get("role")
        parts = []
        for part in item.get("parts") or []:
            if isinstance(part, dict) and "text" in part and role == "model":
                parts.append({"text": sanitize_response(str(part.get("text") or ""))})
            else:
                parts.append(part)
        cleaned.append({"role": role, "parts": parts})
    return cleaned


def preferred_examples_prompt() -> str:
    return SYRIAN_LANGUAGE_GUIDE


def apply_to_app(app) -> None:
    marker = LANGUAGE_POLICY_MARKER
    if marker not in getattr(app, "SYSTEM_INSTRUCTION", ""):
        app.SYSTEM_INSTRUCTION = getattr(app, "SYSTEM_INSTRUCTION", "") + "\n\n" + marker + "\n" + SYRIAN_LANGUAGE_GUIDE
