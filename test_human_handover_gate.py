import handover_gate as gate


def test_handover_gate_blocks_pending_response_after_staff_message():
    state = {"paused": True}
    assert gate.handover_gate(lambda: state["paused"]) is False


def test_handover_gate_allows_response_when_not_paused():
    state = {"paused": False}
    assert gate.handover_gate(lambda: state["paused"]) is True


def test_handover_generation_invalidates_old_worker_response():
    current = gate.HandoverGeneration()
    before = current.snapshot()
    current.bump()
    assert current.is_current(before) is False


def test_staff_message_recipient_is_paused_before_any_bot_response():
    payload = {
        "sender": {"id": "page-employee"},
        "recipient": {"id": "customer-1"},
        "message": {"is_echo": True, "mid": "staff-mid", "text": "أهلاً، كيف بقدر ساعدك؟"},
    }
    assert gate.is_staff_echo(payload, bot_sent_mids={}) is True


def test_bot_echo_is_not_treated_as_staff_message():
    payload = {
        "sender": {"id": "page"},
        "recipient": {"id": "customer-1"},
        "message": {"is_echo": True, "mid": "bot-mid", "text": "الدوام عنا 3 أيام بالأسبوع."},
    }
    assert gate.is_staff_echo(payload, bot_sent_mids={"bot-mid": 1.0}) is False


def test_handover_notice_is_allowed_through_send_gate():
    assert gate.is_handover_notice("تم تحويل المحادثة لفريق المتابعة.") is True
    assert gate.is_handover_notice("أهلاً، هذا رد آلي.") is False
