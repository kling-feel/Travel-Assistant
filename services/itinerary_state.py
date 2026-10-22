"""Display state for owned orders; order status remains the transaction truth."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def display_state(kind, order, now=None):
    """Separate upcoming/in-progress orders from finished and cancelled history."""
    if order["status"] == "cancelled":
        return "cancelled"
    if order["status"] != "confirmed":
        return "other"
    now = now or datetime.now(timezone.utc)
    if kind == "flight":
        return "active" if _utc(order["arrival_at"]) > now else "finished"
    if kind == "hotel":
        zone = order.get("airport_timezone")
        today = now.astimezone(ZoneInfo(zone)).date() if zone else now.date()
        return "active" if order["check_out"] > today else "finished"
    if kind == "rental":
        return "active" if _utc(order["dropoff_at"]) > now else "finished"
    raise ValueError("unknown order kind")
