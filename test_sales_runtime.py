class DummyApp:
    SYSTEM_INSTRUCTION = "BASE"

    def __init__(self, response):
        self._response = response

    def generate_ai_reply(self, sender_id, user_message, intent=None, is_admin=False, message_id=None):
        return self._response


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

    app = DummyApp("حبيبتي غاليتي، أهلاً بكِ.")
    sales_runtime.apply_patch(app)
    reply = app.generate_ai_reply("u1", "مرحبا")
    assert "حبيبتي" not in reply
    assert "غاليتي" not in reply
    assert "أهلاً بكِ" in reply
