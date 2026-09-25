"""Time-scoping helpers.

An order ID can carry several lifecycle rows. The complaint refers to the most recent
purchase at or before `opened_at`; every event/item/payment is scoped to that row's
window [purchase_of_row, purchase_of_next_row).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def as_brl(value: Decimal | int | float | None) -> float | None:
    return None if value is None else float(Decimal(str(value)).quantize(Decimal("0.01")))


ROW_TIMESTAMP_FIELDS = (
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
)


@dataclass(frozen=True)
class OrderScope:
    """One lifecycle row of an order plus the rule that attributes evidence to it.

    Rows with the same purchase timestamp form one group (they are indistinguishable).
    An event belongs to the row whose own timestamps match it exactly; otherwise to the
    row with the latest purchase at or before the event.
    """

    row: dict[str, Any]
    key: datetime | None
    rows: tuple[dict[str, Any], ...]
    source: str  # which tool the scoped row came from

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def contains(self, value: Any) -> bool:
        ts = parse_ts(value)
        if len(self.rows) <= 1 or self.key is None:
            return True
        if ts is None:
            return False
        exact = {
            _purchase(r)
            for r in self.rows
            if any(parse_ts(r.get(f)) == ts for f in ROW_TIMESTAMP_FIELDS)
        }
        if exact:
            return self.key in exact
        earlier = [k for k in (_purchase(r) for r in self.rows) if k is not None and k <= ts]
        return bool(earlier) and max(earlier) == self.key


def _purchase(row: dict[str, Any]) -> datetime | None:
    return parse_ts(row.get("order_purchase_timestamp"))


def candidate_scopes(
    history_rows: list[dict[str, Any]],
    order_id: str,
    opened_at: str | None,
    fallback_row: dict[str, Any] | None,
) -> list[OrderScope]:
    """Distinct lifecycle rows purchased at or before opened_at, most recent first.

    The first scope is the default; the rest are alternatives the policy agent may pick
    when only they carry evidence for the customer's claim.
    """
    rows = tuple(
        sorted(
            (r for r in history_rows if r.get("order_id") == order_id and _purchase(r)),
            key=_purchase,
        )
    )
    if not rows:
        if fallback_row is None:
            return []
        return [OrderScope(fallback_row, _purchase(fallback_row), (fallback_row,), "get_order")]
    opened = parse_ts(opened_at)
    eligible = [r for r in rows if opened is None or _purchase(r) <= opened] or [rows[0]]
    scopes: list[OrderScope] = []
    seen: set[datetime | None] = set()
    for row in reversed(eligible):
        key = _purchase(row)
        if key not in seen:
            seen.add(key)
            scopes.append(OrderScope(row, key, rows, "get_customer_history"))
    return scopes
