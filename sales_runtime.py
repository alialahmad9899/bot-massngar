"""Gunicorn runtime guardrails for sales conversation behavior."""
from __future__ import annotations

import sales_conversation_policy as policy

PATCH_MARKER = "_sales_conversation_runtime_v1"


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

    setattr(app_module, PATCH_MARKER, True)
    print("[SALES_POLICY] professional/deferred-booking guard enabled", flush=True)
    return app_module
