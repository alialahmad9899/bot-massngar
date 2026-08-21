class DummyApp:
    SYSTEM_INSTRUCTION = "BASE"
    BOT_SENT_MIDS = {"bot-mid-1": 1.0}

    def __init__(self, response):
        self._response = response
        self.paused = []
        self.sent = []
        self.original_calls = 0
        self.saved = []

    def generate_ai_reply(self, sender_id, user_message, intent=None, is_admin=False, message_id=None):
        return self._response

    def set_handover_status(self, sender_id, status=1):
        self.paused.append((sender_id, status))

    def send_facebook_message(self, recipient_id, message_text, quick_replies=None):
        self.sent.append((recipient_id, message_text))
        return True

    def process_single_message(self, event_payload, event_id=None):
        self.original_calls += 1

    def save_message_db(self, sender_id, role, content, intent=None, message_id=None):
        self.saved.append((sender_id, role, content, intent, message_id))


def test_runtime_blocks_premature_booking_call_to_action():
    import sales_runtime

    app = DummyApp("أوقات الدوام مرنة. حابة نحجز مكانك ونثبت التسجيل عبر شام كاش؟")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "اوقات الدوام كيف")
    assert "شام كاش" not in reply
    assert "نحجز" not in reply


def test_runtime_allows_payment_after_explicit_booking():
    import sales_runtime

    app = DummyApp("ممتاز، فيكي تثبتي المقعد عبر شام كاش.")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "بدي احجز دورة الميك اب")
    assert "شام كاش" in reply


def test_runtime_sanitizes_affectionate_terms_for_customer():
    import sales_runtime

    app = DummyApp("حبيبتي غاليتي، بابا، يا قلبي، روحي، عيونك. أهلاً بكِ.")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "مرحبا")
    for banned in ("حبيبتي", "غاليتي", "بابا", "يا قلبي", "روحي", "عيونك"):
        assert banned not in reply
    assert "أهلاً بكِ" in reply


def test_runtime_uses_professional_canned_reply_for_short_thanks():
    import sales_runtime

    app = DummyApp("عيونك يا قمر، على راسي.")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "يسلموا")
    assert reply == "العفو، أهلاً بكِ."
    assert any(row[1] == "model" and row[2] == reply for row in app.saved)


def test_runtime_injects_syrian_language_guide():
    import sales_runtime

    app = DummyApp("تمام")
    sales_runtime.apply_patch(app)
    assert "دليل الكتابة السورية العامية المهنية" in app.SYSTEM_INSTRUCTION
    for word in ("شو", "هيك", "لانو", "هلق", "فيكي", "بتقدري", "ما في"):
        assert word in app.SYSTEM_INSTRUCTION


def test_runtime_silences_customer_after_human_echo():
    import sales_runtime

    app = DummyApp("رد آلي لا يجب أن يظهر")
    sales_runtime.apply_patch(app)
    event = {
        "sender": {"id": "PAGE_ID"},
        "recipient": {"id": "CUSTOMER_1"},
        "message": {"mid": "human-mid-1", "is_echo": True, "text": "أهلاً، معك الإدارة"},
    }
    app.process_single_message(event)
    assert app.paused == [("CUSTOMER_1", 1)]
    assert app.original_calls == 0


def test_runtime_does_not_pause_for_bot_echo():
    import sales_runtime

    app = DummyApp("ignored")
    sales_runtime.apply_patch(app)
    event = {
        "sender": {"id": "PAGE_ID"},
        "recipient": {"id": "CUSTOMER_1"},
        "message": {"mid": "bot-mid-1", "is_echo": True, "text": "أهلاً بكِ"},
    }
    app.process_single_message(event)
    assert app.paused == []
    assert app.original_calls == 1


def test_runtime_silences_explicit_management_request_before_ai():
    import sales_runtime

    app = DummyApp("not used")
    sales_runtime.apply_patch(app)
    event = {
        "sender": {"id": "CUSTOMER_1"},
        "message": {"mid": "customer-mid-1", "text": "بدي اتواصل مع الإدارة"},
    }
    app.process_single_message(event)
    assert app.paused == [("CUSTOMER_1", 1)]
    assert app.original_calls == 0
    assert app.sent


def test_runtime_history_wrapper_sanitizes_previous_model_messages():
    import sales_runtime

    app = DummyApp("تمام")

    def history(sender_id, limit=12):
        return [
            {"role": "user", "parts": [{"text": "مرحبا"}]},
            {"role": "model", "parts": [{"text": "حبيبتي بابا، شو حابة تعرفي؟"}]},
        ]

    app.get_user_history_db = history
    sales_runtime.apply_patch(app)
    cleaned = app.get_user_history_db("u1")
    assert "حبيبتي" not in cleaned[1]["parts"][0]["text"]
    assert "بابا" not in cleaned[1]["parts"][0]["text"]
