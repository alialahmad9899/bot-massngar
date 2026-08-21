import sys
import types


class DummyApp:
    DB_LOCK = None
    BOT_SENT_MIDS = {}


def load_runtime():
    import sales_runtime
    return sales_runtime


def test_schedule_question_has_no_booking_cta():
    policy = load_runtime()
    reply = policy.guard_response(
        "اوقات الدوام كيف",
        "أوقات الدوام من 10 إلى 5. حابة نحجز مكانك ونثبت التسجيل عبر شام كاش؟",
    )
    assert "شام كاش" not in reply
    assert "نحجز" not in reply
    assert "تثبيت" not in reply


def test_new_affection_terms_are_sanitized():
    policy = load_runtime()
    reply = policy.sanitize_professional_response(
        "حبيبتي يا غالية يا قلبي يا روحي بابا، أهلاً بكِ"
    )
    for term in ("حبيبتي", "غالية", "قلبي", "روحي", "بابا"):
        assert term not in reply


def test_human_echo_is_detected_as_staff_message():
    policy = load_runtime()
    app = DummyApp()
    app.BOT_SENT_MIDS["bot-mid-1"] = 1.0
    human_event = {
        "sender": {"id": "PAGE_ID"},
        "recipient": {"id": "CUSTOMER_1"},
        "message": {"mid": "human-mid-1", "is_echo": True, "text": "أهلاً، معك الإدارة"},
    }
    assert policy.is_human_page_echo(human_event, bot_sent_mids=app.BOT_SENT_MIDS) is True


def test_bot_echo_is_not_treated_as_human():
    policy = load_runtime()
    event = {
        "sender": {"id": "PAGE_ID"},
        "recipient": {"id": "CUSTOMER_1"},
        "message": {"mid": "bot-mid-1", "is_echo": True, "text": "أهلاً بكِ"},
    }
    assert policy.is_human_page_echo(event, bot_sent_mids={"bot-mid-1": 1.0}) is False


def test_human_mode_keywords_cover_management_request():
    policy = load_runtime()
    assert policy.has_handover_intent("بدي اتواصل مع الإدارة") is True
    assert policy.has_handover_intent("بدي موظف يحكي معي") is True
    assert policy.has_handover_intent("خلي حدا من الإدارة يتواصل معي") is True
