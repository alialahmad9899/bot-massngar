import production_runtime_v2 as runtime


def test_social_replies_are_deterministic():
    assert runtime.social("يسلموا") == "العفو، أهلاً بكِ."
    assert runtime.social("شكراً") == "العفو، أهلاً بكِ."


def test_sanitizer_removes_affectionate_and_institutional_language():
    text = "حبيبتي وغاليتي، يسعدنا دائماً تواصلك معنا. إن شاء الله أهلاً وسهلاً بكِ."
    cleaned = runtime.sanitize(text)
    assert "حبيبتي" not in cleaned
    assert "غاليتي" not in cleaned
    assert "يسعدنا دائماً" not in cleaned
    assert "إن شاء الله أهلاً وسهلاً" not in cleaned
    assert "أهلاً بكِ" in cleaned


def test_sanitizer_removes_unrequested_titles():
    assert runtime.sanitize("أستاذ، أهلاً بكِ.").startswith("أهلاً")
    assert runtime.sanitize("مدام، أهلاً بكِ.").startswith("أهلاً")


def test_enrollment_and_handover_intent_boundaries():
    assert runtime.enrollment("بدي احجز دورة الميك اب")
    assert runtime.enrollment("كيف ثبت مقعدي؟")
    assert not runtime.enrollment("اوقات الدوام كيف")
    assert runtime.handover("بدي اتواصل مع الإدارة")
    assert runtime.handover("بدي موظف يحكي معي")
    assert not runtime.handover("شو عنوان الإدارة؟")


def test_payment_question_is_not_enrollment():
    assert runtime.payment_question("شو طرق الدفع؟")
    assert not runtime.enrollment("شو طرق الدفع؟")


def test_sales_guard_blocks_premature_cta():
    user = "اوقات الدوام كيف"
    response = "الدوام عنا 3 أيام بالأسبوع. حابة تحجزي وتثبتي المقعد عبر شام كاش؟"
    guarded = runtime.sales_guard(user, response)
    assert "شام كاش" not in guarded
    assert "تحجزي" not in guarded


def test_sales_guard_allows_explicit_booking_flow():
    user = "بدي احجز دورة الميك اب"
    response = "ممتاز، فيكي تثبتي المقعد عبر شام كاش."
    assert "شام كاش" in runtime.sales_guard(user, response)


def test_durable_message_key_is_stable_without_mid():
    event = {"sender": {"id": "u1"}, "message": {"text": "مرحبا"}}
    assert runtime.durable_message_key(event) == runtime.durable_message_key(event)


def test_banned_terms_have_replacements():
    for term in runtime.BANNED:
        assert runtime.BANNED[term]
