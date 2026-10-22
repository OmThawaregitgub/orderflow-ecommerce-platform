"""Database-backed transactional outbox processor used by the separate worker."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Order, OrderStatus, OutboxEvent, OutboxStatus, utcnow

logger = logging.getLogger("orderflow.worker")


def process_one(db: Session) -> bool:
    """Claim and process one event. Returns False when the queue is empty."""
    now = utcnow()

    # Reclaim leases left behind by a worker process that crashed mid-task.
    lease_deadline = now - timedelta(minutes=5)
    db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PROCESSING, OutboxEvent.claimed_at < lease_deadline)
        .values(status=OutboxStatus.PENDING, claimed_at=None, last_error="Recovered expired worker lease")
    )
    db.commit()

    event = db.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PENDING, OutboxEvent.available_at <= now)
        .order_by(OutboxEvent.id)
        .limit(1)
    )
    if event is None:
        return False

    event_id = event.id
    claim = db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id == event_id, OutboxEvent.status == OutboxStatus.PENDING)
        .values(status=OutboxStatus.PROCESSING, attempts=OutboxEvent.attempts + 1, claimed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        db.rollback()
        return True
    db.commit()

    try:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id))
        if event is None:
            return True
        if event.event_type == "order.created":
            order = db.scalar(select(Order).where(Order.id == event.aggregate_id))
            # An order might have been cancelled before this event was delivered.
            if order and order.status == OrderStatus.PENDING:
                order.status = OrderStatus.PROCESSING
                order.updated_at = utcnow()
            logger.info("Handled order.created", extra={"order_number": event.payload.get("order_number")})
        elif event.event_type == "order.cancelled":
            logger.info("Handled order.cancelled", extra={"order_number": event.payload.get("order_number")})
        elif event.event_type == "order.shipped":
            logger.info("Handled order.shipped", extra={"order_number": event.payload.get("order_number")})
        else:
            raise ValueError(f"Unsupported event type: {event.event_type}")

        event.status = OutboxStatus.PROCESSED
        event.processed_at = utcnow()
        event.claimed_at = None
        event.last_error = None
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - error must be persisted for operator visibility
        db.rollback()
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id))
        if event is not None:
            attempts = event.attempts
            event.claimed_at = None
            event.last_error = str(exc)[:2000]
            if attempts >= settings.worker_max_attempts:
                event.status = OutboxStatus.FAILED
            else:
                event.status = OutboxStatus.PENDING
                delay = settings.worker_retry_base_seconds * (2 ** max(0, attempts - 1))
                event.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            db.commit()
        logger.exception("Outbox event processing failed", extra={"event_id": event_id, "attempts": attempts if event else None})
        return True
