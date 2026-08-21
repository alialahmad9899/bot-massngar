"""Runtime guardrails for sales conversation, Syrian Arabic style and handover."""
from __future__ import annotations

import sales_conversation_policy as policy
import sales_language_guide as language

PATCH_MARKER = "_sales_language_and_handover_runtime_v1"


def _handover_notice(app_module, recipient_id):
    sender = getattr(app_module, "send_facebook_message", None)
    if callable(sender):
        sender(
            recipient_id,
            "تم تحويل المحادثة إلى فريق المتابعة. من الآن سيتولى أحد أعضاء الفريق التواصل معكِ مباشرة.",
        )


def _pause_for_human_echo(app_module, event_payload):
    if not policy.is_human_page_echo(event_payload, getattr(app_module, "BOT_SENT_MIDS", {})):
        return False
    recipient = (event_payload.get("recipient") or {}).get("id")
    if recipient and callable(getattr(app_module, "set_handover_status", None)):
        app_module.set_handover_status(recipient, 1)
        print(f"[HANDOVER] staff message detected; bot paused for user={recipient}", flush=True)
    return True


def _wrap_process_single_message(app_module, original):
    def wrapped(event_payload, event_id=None):
        if _pause_for_human_echo(app_module, event_payload):
            return

        message = event_payload.get("message") or {}
        if message.get("is_echo"):
            return original(event_payload, event_id=event_id)

        user_message = (message.get("text") or "").strip()
        sender_id = (event_payload.get("sender") or {}).get("id")
        if sender_id and user_message and policy.has_handover_intent(user_message):
            app_module.set_handover_status(sender_id, 1)
            _handover_notice(app_module, sender_id)
            print(f"[HANDOVER] explicit human request; bot paused for user={sender_id}", flush=True)
            return

        return original(event_payload, event_id=event_id)

    return wrapped


def _wrap_generate(app_module, original):
    def guarded_generate(sender_id, user_message, intent=None, is_admin=False, message_id=None):
        # Keep management/admin answers clean too, but do not apply customer CTA filtering to admin mode.
        if not is_admin:
            canned = language.social_reply(user_message)
            if canned:
                try:
                    app_module.save_message_db(sender_id, "user", user_message, intent=intent, message_id=message_id)
                    app_module.save_message_db(sender_id, "model", canned)
                except Exception as exc:
                    print(f"[LANGUAGE] canned-reply persistence failed: {exc}", flush=True)
                return canned

        response = original(
            sender_id,
            user_message,
            intent=intent,
            is_admin=is_admin,
            message_id=message_id,
        )
        response = language.sanitize_response(response)
        if is_admin:
            return response
        return policy.guard_response(user_message, response)

    return guarded_generate


def _wrap_history(original):
    def wrapped(sender_id, limit=12):
        history = original(sender_id, limit=limit)
        return language.sanitize_history(history)

    return wrapped


def apply_patch(app_module=None):
    if app_module is None:
        import app as app_module

    if getattr(app_module, PATCH_MARKER, False):
        return app_module

    policy.apply_to_app(app_module)
    language.apply_to_app(app_module)

    original_history = getattr(app_module, "get_user_history_db", None)
    if callable(original_history):
        app_module.get_user_history_db = _wrap_history(original_history)

    original_generate = getattr(app_module, "generate_ai_reply", None)
    if callable(original_generate):
        app_module.generate_ai_reply = _wrap_generate(app_module, original_generate)

    original_process = getattr(app_module, "process_single_message", None)
    if callable(original_process):
        app_module.process_single_message = _wrap_process_single_message(app_module, original_process)

    setattr(app_module, PATCH_MARKER, True)
    print("[SALES_POLICY] Syrian language + professional style + deferred booking + human handover enabled", flush=True)
    return app_module
