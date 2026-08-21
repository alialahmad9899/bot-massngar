"""Gunicorn runtime guardrails for sales conversation behavior."""
from __future__ import annotations

import sales_conversation_policy as policy

PATCH_MARKER = "_sales_conversation_runtime_v2"


def _handover_notice(app_module, recipient_id):
    sender = getattr(app_module, "send_facebook_message", None)
    if callable(sender):
        sender(
            recipient_id,
            "تم تحويل المحادثة إلى فريق المتابعة. من الآن سيتولى أحد أعضاء الفريق التواصل معك مباشرة.",
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
        # A real Page echo that did not originate from the bot means a human has entered the chat.
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


def apply_patch(app_module=None):
    if app_module is None:
        import app as app_module

    if getattr(app_module, PATCH_MARKER, False):
        return app_module

    policy.apply_to_app(app_module)

    original_generate = getattr(app_module, "generate_ai_reply", None)
    if callable(original_generate):
        def guarded_generate(sender_id, user_message, intent=None, is_admin=False, message_id=None):
            response = original_generate(
                sender_id,
                user_message,
                intent=intent,
                is_admin=is_admin,
                message_id=message_id,
            )
            if is_admin:
                return policy.sanitize_professional_response(response)
            return policy.guard_response(user_message, response)

        app_module.generate_ai_reply = guarded_generate

    original_process = getattr(app_module, "process_single_message", None)
    if callable(original_process):
        app_module.process_single_message = _wrap_process_single_message(app_module, original_process)

    setattr(app_module, PATCH_MARKER, True)
    print("[SALES_POLICY] professional/deferred-booking + human-handover guard enabled", flush=True)
    return app_module
