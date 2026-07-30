from application.runtime_notification_adapters import build_runtime_notification_adapters


def test_runtime_notification_adapter_records_negative_ack_as_failed():
    events = []
    adapters = build_runtime_notification_adapters(
        send_message=lambda _message: False,
        delivery_events=events,
        log_message=lambda _message: None,
    )

    sent = adapters.publish_cycle_notification(
        detailed_text="details",
        compact_text="rebalance",
    )

    assert sent is False
    assert events[0]["delivery_status"] == "failed"
    assert events[0]["transport_acknowledged"] is False
    assert "compact_text" not in events[0]


def test_runtime_notification_adapter_records_sender_exception_without_raising():
    events = []

    def fail(_message):
        raise RuntimeError("transport unavailable")

    adapters = build_runtime_notification_adapters(
        send_message=fail,
        delivery_events=events,
        log_message=lambda _message: None,
    )

    sent = adapters.publish_cycle_notification(
        detailed_text="details",
        compact_text="rebalance",
    )

    assert sent is False
    assert events[0]["delivery_status"] == "failed"
    assert events[0]["error_type"] == "RuntimeError"
