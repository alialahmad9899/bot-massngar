"""Production WSGI entrypoint with strict runtime overrides."""
from __future__ import annotations

import re
import sys

import app as app_module
import admin_runtime_compat
import production_runtime_v2
import handover_gate

# Production and tests use the same canonical admin backend.
sys.modules["admin_runtime"] = admin_runtime_compat

_original_sanitize = production_runtime_v2.sanitize
_original_handover = production_runtime_v2.handover


def _strict_sanitize(text: str) -> str:
    value = _original_sanitize(text)
    for title in ("أستاذة", "أستاذ", "مدام", "آنسة", "استاذة", "استاذ", "انسة"):
        value = re.sub(rf"(?<!\w){re.escape(title)}(?!\w)", "", value)
    value = value.replace("إن شاء الله", "").replace("ان شاء الله", "")
    value = re.sub(r"\s+", " ", value).strip(" .،")
    return value


def _strict_handover(text: str) -> bool:
    normalized = production_runtime_v2.normalize(text)
    if _original_handover(text):
        return True
    patterns = (
        r"\bخلي\s+حدا\s+من\s+الادارة\s+(?:يتواصل|يحكي)\s+معي\b",
        r"\bخلي\s+حدا\s+من\s+الاداره\s+(?:يتواصل|يحكي)\s+معي\b",
        r"\bخلي\s+الادارة\s+(?:تتواصل|تحكي)\s+معي\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


production_runtime_v2.sanitize = _strict_sanitize
production_runtime_v2.handover = _strict_handover
production_runtime_v2.bootstrap(app_module)

# Canonical WSGI object exported for Gunicorn/Render.
app = app_module.app

# Final safety boundary: no bot message can leave the process while a human owns the conversation.
handover_gate.install(app)

__all__ = ["app"]
