import production_runtime_v2 as runtime


def test_handover_gate_blocks_pending_response_after_staff_message():
    state = {"paused": True}
    assert runtime.handover_gate(lambda: state["paused"]) is False


def test_handover_gate_allows_response_when_not_paused():
    state = {"paused": False}
    assert runtime.handover_gate(lambda: state["paused"]) is True


def test_handover_generation_invalidates_old_worker_response():
    current = runtime.HandoverGeneration()
    before = current.snapshot()
    current.bump()
    assert current.is_current(before) is False


def test_staff_message_recipient_is_paused_before_any_bot_response():
    payload = {
        "sender": {"id": "page-employee"},
        "recipient": {"id": "customer-1"},
        "message": {"is_echo": True, "mid": "staff-mid", "text": "أهلاً، كيف بقدر ساعدك؟"},
    }
    assert runtime.is_staff_echo(payload, bot_sent_mids={}) is True
