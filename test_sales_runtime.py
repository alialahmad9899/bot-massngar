class DummyApp:
    SYSTEM_INSTRUCTION = "BASE"
    BOT_SENT_MIDS = {"bot-mid-1": 1.0}

    def __init__(self, response):
        self._response = response
        self.paused = []
        self.sent = []
        self.original_calls = 0

    def generate_ai_reply(self, sender_id, user_message, intent=None, is_admin=False, message_id=None):
        return self._response

    def set_handover_status(self, sender_id, status=1):
        self.paused.append((sender_id, status))

    def send_facebook_message(self, recipient_id, message_text, quick_replies=None):
        self.sent.append((recipient_id, message_text))
        return True

    def process_single_message(self, event_payload, event_id=None):
        self.original_calls += 1


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

    app = DummyApp("حبيبتي غاليتي، بابا، يا قلبي. أهلاً بكِ.")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "مرحبا")
    assert "حبيبتي" not in reply
    assert "غاليتي" not in reply
    assert "بابا" not in reply
    assert "يا قلبي" not in reply
    assert "أهلاً بكِ" in reply


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
