"""Last-mile gate preventing bot messages while a human owns a conversation."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class HandoverGeneration:
    _value: int = 0

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def snapshot(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value

    def is_current(self, snapshot: int) -> bool:
        with self._lock:
            return snapshot == self._value


def handover_gate(is_paused: Callable[[], bool]) -> bool:
    """Return True only when the bot is allowed to send."""
    try:
        return not bool(is_paused())
    except Exception:
        return False


def is_staff_echo(event: dict[str, Any], bot_sent_mids: dict[str, float] | None = None) -> bool:
    message = event.get("message") or {}
    if not message.get("is_echo"):
        return False
    mid = message.get("mid")
    return not bool(mid and bot_sent_mids and mid in bot_sent_mids)


def is_handover_notice(text: str | None) -> bool:
    value = re.sub(r"\s+", " ", (text or "").strip())
    return value.startswith("تم تحويل المحادثة")


def install(app: Any) -> Any:
    """Install the last-mile handover gate on the live Flask app."""
    marker = "_HANDOVER_SEND_GATE_INSTALLED"
    if getattr(app, marker, False):
        return app

    original_send = app.send_facebook_message
    original_set_handover = app.set_handover_status
    generation = HandoverGeneration()
    local = threading.local()
    app.handover_generation = generation

    def set_handover_with_generation(sender_id, status=1):
        result = original_set_handover(sender_id, status)
        generation.bump()
        return result

    def send_with_gate(recipient_id, message_text, quick_replies=None):
        bypass = bool(getattr(local, "bypass", False))
        if bypass or is_handover_notice(message_text):
            return original_send(recipient_id, message_text, quick_replies)

        snapshot = generation.snapshot()
        if not handover_gate(lambda: app.is_user_paused(recipient_id)):
            print(f"[HANDOVER-GATE] blocked bot send user={recipient_id}", flush=True)
            return True

        if not generation.is_current(snapshot) or not handover_gate(lambda: app.is_user_paused(recipient_id)):
            print(f"[HANDOVER-GATE] blocked stale response user={recipient_id}", flush=True)
            return True

        return original_send(recipient_id, message_text, quick_replies)

    def send_bypassing_gate(recipient_id, message_text, quick_replies=None):
        previous = bool(getattr(local, "bypass", False))
        local.bypass = True
        try:
            return send_with_gate(recipient_id, message_text, quick_replies)
        finally:
            local.bypass = previous

    app.set_handover_status = set_handover_with_generation
    app.send_facebook_message = send_with_gate
    app.send_facebook_message_bypass_handover_gate = send_bypassing_gate
    setattr(app, marker, True)
    return app
