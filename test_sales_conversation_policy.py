import importlib.util
import os
from pathlib import Path


class _DummyApp:
    SYSTEM_INSTRUCTION = "BASE SYSTEM"


def load_policy_module():
    path = Path(__file__).with_name("sales_conversation_policy.py")
    spec = importlib.util.spec_from_file_location("sales_conversation_policy_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schedule_question_is_not_booking_intent():
    policy = load_policy_module()
    assert policy.has_enrollment_intent("اوقات الدوام كيف") is False
    assert policy.has_enrollment_intent("شو مواعيد الدوام؟") is False
    assert policy.should_offer_payment("اوقات الدوام كيف") is False


def test_explicit_booking_intent_allows_payment_flow():
    policy = load_policy_module()
    assert policy.has_enrollment_intent("بدي احجز دورة الميك اب") is True
    assert policy.has_enrollment_intent("بدي سجل بالدورة") is True
    assert policy.has_enrollment_intent("كيف ثبت مقعدي؟") is True
    assert policy.should_offer_payment("كيف ثبت مقعدي؟") is True


def test_information_question_about_payment_can_be_answered_without_cta():
    policy = load_policy_module()
    assert policy.should_offer_payment("شو طرق الدفع المتاحة؟") is True
    assert policy.has_enrollment_intent("شو طرق الدفع المتاحة؟") is False


def test_professional_sanitizer_blocks_affectionate_terms():
    policy = load_policy_module()
    text = "حبيبتي غاليتي، أهلاً بكِ. يا غالي فيني ساعدك."
    cleaned = policy.sanitize_professional_response(text)
    banned = ["حبيبتي", "حبيبي", "غاليتي", "غالية", "غالي", "يا غالي", "يا حبيب"]
    assert not any(token in cleaned for token in banned)
    assert "أهلاً بكِ" in cleaned or "أهلاً بك" in cleaned


def test_system_policy_is_appended_once():
    policy = load_policy_module()
    app = _DummyApp()
    policy.apply_to_app(app)
    first = app.SYSTEM_INSTRUCTION
    policy.apply_to_app(app)
    assert app.SYSTEM_INSTRUCTION == first
    assert "التثبيت" in first
    assert "شام كاش" in first
    assert "حبيبي" in first
