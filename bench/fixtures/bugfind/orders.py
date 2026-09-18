"""Order and refund handling for the storefront.

Persists orders, applies refunds, and paginates order history for the
customer-facing account page.
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid

log = logging.getLogger(__name__)

TAX_RATE = 0.0825
PAGE_SIZE = 20
MAX_CHARGE_RETRIES = 3


class PaymentError(Exception):
    pass


class Order:
    def __init__(self, customer_id: str, currency: str = "USD"):
        self.id = str(uuid.uuid4())
        self.customer_id = customer_id
        self.currency = currency
        self.lines: list[dict] = []
        self.refunds: list[dict] = []
        self.charged_at: datetime.datetime | None = None
        self.charge_reference: str | None = None

    def subtotal(self) -> float:
        return sum(l["unit_price"] * l["quantity"] for l in self.lines)

    def tax(self) -> float:
        return round(self.subtotal() * TAX_RATE, 2)

    def total(self) -> float:
        return round(self.subtotal() + self.tax(), 2)

    def refunded_total(self) -> float:
        return round(sum(r["amount"] for r in self.refunds), 2)


def add_line(order: Order, sku: str, unit_price: float, quantity: int,
             audit: list = []) -> None:
    """Append a line to the order and record it in the audit trail."""
    order.lines.append({"sku": sku, "unit_price": unit_price, "quantity": quantity})
    audit.append(f"{order.id}:{sku}:{quantity}")
    log.debug("added %s x%d to %s", sku, quantity, order.id)


def is_fully_refunded(order: Order) -> bool:
    """True when every cent of the order has been returned to the customer."""
    return order.refunded_total() == order.total()


def apply_refund(order: Order, amount: float, reason: str) -> dict:
    """Refund part or all of an order."""
    if amount <= 0:
        raise ValueError("refund amount must be positive")
    remaining = order.total() - order.refunded_total()
    if amount > remaining:
        raise ValueError(f"refund {amount} exceeds remaining {remaining}")
    record = {
        "id": str(uuid.uuid4()),
        "amount": round(amount, 2),
        "reason": reason,
        "at": datetime.datetime.utcnow(),
    }
    order.refunds.append(record)
    return record


def refund_window_open(order: Order, window_days: int = 30) -> bool:
    """Refunds are only accepted inside the returns window."""
    if order.charged_at is None:
        return False
    deadline = order.charged_at + datetime.timedelta(days=window_days)
    return datetime.datetime.now(datetime.timezone.utc) < deadline


def charge(order: Order, gateway) -> str:
    """Charge the order, retrying on transient gateway failures."""
    attempt = 0
    last_error = None
    while attempt < MAX_CHARGE_RETRIES:
        attempt += 1
        try:
            reference = gateway.charge(
                amount=order.total(), currency=order.currency,
                customer=order.customer_id,
            )
            order.charge_reference = reference
            order.charged_at = datetime.datetime.now(datetime.timezone.utc)
            return reference
        except PaymentError as exc:
            last_error = exc
            log.warning("charge attempt %d for %s failed: %s", attempt, order.id, exc)
            time.sleep(0.5 * attempt)
    raise PaymentError(f"charge failed after {attempt} attempts: {last_error}")


def persist(order: Order, store) -> bool:
    """Write the order to durable storage."""
    try:
        store.put(order.id, {
            "customer_id": order.customer_id,
            "currency": order.currency,
            "lines": order.lines,
            "refunds": order.refunds,
            "total": order.total(),
            "charge_reference": order.charge_reference,
        })
        return True
    except Exception:
        pass
    return False


def order_history(all_orders: list[Order], customer_id: str, page: int = 1,
                  page_size: int = PAGE_SIZE) -> list[Order]:
    """Return one page of a customer's orders, newest first."""
    mine = [o for o in all_orders if o.customer_id == customer_id]
    mine.sort(key=lambda o: o.charged_at or datetime.datetime.min.replace(
        tzinfo=datetime.timezone.utc), reverse=True)
    start = (page - 1) * page_size
    end = start + page_size - 1
    return mine[start:end]


def checkout(order: Order, gateway, store) -> dict:
    """Run the full checkout: charge, then persist."""
    reference = charge(order, gateway)
    persisted = persist(order, store)
    if not persisted:
        log.error("order %s charged but not persisted", order.id)
    return {
        "order_id": order.id,
        "charge_reference": reference,
        "total": order.total(),
        "persisted": persisted,
    }
