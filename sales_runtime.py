"""Compatibility shim for the unified production runtime.

Production uses production_runtime_v2 directly. This module remains only so old
imports/tests resolve to the same canonical behavior.
"""
from __future__ import annotations

import production_runtime_v2 as runtime

PATCH_MARKER = "_sales_runtime_compat_v2"


def guard_response(user_message: str, response_text: str) -> str:
    return runtime.sales_guard(user_message, response_text)


def sanitize_professional_response(text: str) -> str:
    return runtime.sanitize(text)


def has_handover_intent(text: str) -> bool:
    return runtime.handover(text)


def is_human_page_echo(event: dict, bot_sent_mids: dict | None = None) -> bool:
    message = event.get("message") or {}
    if not message.get("is_echo"):
        return False
    mid = message.get("mid")
    return not bool(mid and bot_sent_mids and mid in bot_sent_mids)


def _wrap_generate(app_module, original):
    def guarded(sender_id, user_message, intent=None, is_admin=False, message_id=None):
        if not is_admin:
            canned = runtime.social(user_message)
            if canned:
                try:
                    app_module.save_message_db(sender_id, "user", user_message, intent=intent, message_id=message_id)
                    app_module.save_message_db(sender_id, "model", canned)
                except Exception:
                    pass
                return canned
        response = original(sender_id, user_message, intent=intent, is_admin=is_admin, message_id=message_id)
        return runtime.sanitize(response) if is_admin else runtime.sales_guard(user_message, response)
    return guarded


def _wrap_history(original):
    def guarded(sender_id, limit=12):
        history = original(sender_id, limit=limit)
        cleaned = []
        for item in history or []:
            role = item.get("role")
            parts = []
            for part in item.get("parts") or []:
                if isinstance(part, dict) and "text" in part and role == "model":
                    parts.append({"text": runtime.sanitize(str(part.get("text") or ""))})
                else:
                    parts.append(part)
            cleaned.append({"role": role, "parts": parts})
        return cleaned
    return guarded


def _wrap_process(app_module, original):
    def guarded(event_payload, event_id=None):
        message = event_payload.get("message") or {}
        if is_human_page_echo(event_payload, getattr(app_module, "BOT_SENT_MIDS", {})):
            recipient = (event_payload.get("recipient") or {}).get("id")
            if recipient and callable(getattr(app_module, "set_handover_status", None)):
                app_module.set_handover_status(recipient, 1)
            return
        if message.get("is_echo"):
            return original(event_payload, event_id=event_id)
        text = (message.get("text") or "").strip()
        sender = (event_payload.get("sender") or {}).get("id")
        if sender and runtime.handover(text):
            app_module.set_handover_status(sender, 1)
            if callable(getattr(app_module, "send_facebook_message", None)):
                app_module.send_facebook_message(sender, "تم تحويل المحادثة لفريق المتابعة. من الآن سيتولى أحد أعضاء الفريق التواصل معكِ مباشرة.")
            return
        return original(event_payload, event_id=event_id)
    return guarded


def apply_patch(app_module=None):
    if app_module is None:
        import app as app_module
    if getattr(app_module, PATCH_MARKER, False):
        return app_module
    app_module.SYSTEM_INSTRUCTION = app_module.SYSTEM_INSTRUCTION + "\n\n" + runtime.SYRIAN_GUIDE
    original_history = getattr(app_module, "get_user_history_db", None)
    original_generate = getattr(app_module, "generate_ai_reply", None)
    original_process = getattr(app_module, "process_single_message", None)
    if callable(original_history):
        app_module.get_user_history_db = _wrap_history(original_history)
    if callable(original_generate):
        app_module.generate_ai_reply = _wrap_generate(app_module, original_generate)
    if callable(original_process):
        app_module.process_single_message = _wrap_process(app_module, original_process)
    setattr(app_module, PATCH_MARKER, True)
    return app_module
