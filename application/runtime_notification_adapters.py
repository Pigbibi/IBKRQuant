"""Builder helpers for IBKR runtime notification adapters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from notifications.events import NotificationPublisher, RenderedNotification
from quant_platform_kit.common.port_adapters import CallableNotificationPort
from quant_platform_kit.common.ports import NotificationPort


@dataclass(frozen=True)
class IBKRNotificationAdapters:
    notification_port: NotificationPort
    cycle_publisher: NotificationPublisher
    delivery_events: list[dict[str, Any]]

    def publish_cycle_notification(self, *, detailed_text: str, compact_text: str) -> bool:
        before_count = len(self.delivery_events)
        outcome = self.cycle_publisher.publish(
            RenderedNotification(
                detailed_text=detailed_text,
                compact_text=compact_text,
            )
        )
        deliveries = self.delivery_events[before_count:]
        if deliveries:
            return all(event.get("delivery_status") == "sent" for event in deliveries)
        return outcome is not False


def build_runtime_notification_adapters(
    *,
    send_message,
    notification_channel: str = "telegram",
    log_message=None,
    delivery_events: list[dict[str, Any]] | None = None,
) -> IBKRNotificationAdapters:
    recorded_delivery_events = delivery_events if delivery_events is not None else []

    def send_recorded_message(message: str) -> bool:
        compact = str(message or "")
        event = {
            "sink": notification_channel,
            "compact_text_sha256": hashlib.sha256(compact.encode("utf-8")).hexdigest(),
            "compact_text_length": len(compact),
        }
        try:
            outcome = send_message(message)
        except Exception as exc:
            event.update(
                {
                    "delivery_status": "failed",
                    "transport_acknowledged": False,
                    "error_type": type(exc).__name__,
                }
            )
            recorded_delivery_events.append(event)
            return False
        acknowledged = outcome is not False
        event.update(
            {
                "delivery_status": "sent" if acknowledged else "failed",
                "transport_acknowledged": acknowledged,
            }
        )
        recorded_delivery_events.append(event)
        return acknowledged

    return IBKRNotificationAdapters(
        notification_port=CallableNotificationPort(send_recorded_message),
        cycle_publisher=NotificationPublisher(
            log_message=log_message or (lambda message: print(message, flush=True)),
            send_message=send_recorded_message,
        ),
        delivery_events=recorded_delivery_events,
    )
