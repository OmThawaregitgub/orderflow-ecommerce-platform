"""Transactional order use cases: idempotency, inventory and cancellation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import (
    InventoryMovement,
    Order,
    OrderItem,
    OrderStatus,
    OutboxEvent,
    Product,
    utcnow,
)
from app.schemas import OrderCreateIn


class OrderError(Exception):
    """Base exception for expected order business-rule failures."""


class ProductUnavailable(OrderError):
    pass


class InsufficientStock(OrderError):
    pass


class IdempotencyConflict(OrderError):
    pass


class OrderNotFound(OrderError):
    pass


class OrderStateConflict(OrderError):
    pass


def _normalised_request(payload: OrderCreateIn) -> tuple[list[tuple[int, int]], str, str]:
    quantities: dict[int, int] = {}
    for line in payload.items:
        quantities[line.product_id] = quantities.get(line.product_id, 0) + line.quantity
    lines = sorted(quantities.items())
    canonical = json.dumps(
        {"customer_email": payload.customer_email.lower(), "items": lines},
        separators=(",", ":"),
        sort_keys=True,
    )
    return lines, payload.customer_email.lower(), hashlib.sha256(canonical.encode()).hexdigest()


def create_order(db: Session, payload: OrderCreateIn, idempotency_key: str) -> tuple[Order, bool]:
    """Create order and reserve stock atomically. Return (order, replayed)."""
    if not idempotency_key or len(idempotency_key) > 128:
        raise OrderError("A valid Idempotency-Key header (1–128 characters) is required.")
    lines, customer_email, request_hash = _normalised_request(payload)
    existing = db.scalar(select(Order).where(Order.idempotency_key == idempotency_key).options(selectinload(Order.items)))
    if existing:
        if existing.request_hash != request_hash:
            raise IdempotencyConflict("This idempotency key was already used for a different request.")
        return existing, True

    order = Order(
        order_number=f"OF-{uuid4().hex[:12].upper()}",
        customer_email=customer_email,
        status=OrderStatus.PENDING,
        total_amount=Decimal("0.00"),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    total = Decimal("0.00")
    try:
        db.add(order)
        db.flush()

        # Stable lock/update order reduces deadlock risk on databases that use row locks.
        for product_id, quantity in lines:
            product = db.scalar(select(Product).where(Product.id == product_id))
            if product is None or not product.is_active:
                raise ProductUnavailable(f"Product {product_id} does not exist or is inactive.")
            if quantity > 100:
                raise InsufficientStock("Maximum quantity per product is 100.")

            result = db.execute(
                update(Product)
                .where(Product.id == product_id, Product.is_active.is_(True), Product.stock >= quantity)
                .values(stock=Product.stock - quantity, updated_at=utcnow())
                .returning(Product.stock)
                .execution_options(synchronize_session=False)
            )
            stock_after = result.scalar_one_or_none()
            if stock_after is None:
                raise InsufficientStock(f"Not enough stock for {product.name}; available stock may have changed.")

            unit_price = Decimal(product.price).quantize(Decimal("0.01"))
            line_total = (unit_price * quantity).quantize(Decimal("0.01"))
            total += line_total
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    sku_snapshot=product.sku,
                    product_name_snapshot=product.name,
                    unit_price=unit_price,
                    quantity=quantity,
                    line_total=line_total,
                )
            )
            db.add(
                InventoryMovement(
                    product_id=product.id,
                    order_id=order.id,
                    quantity_delta=-quantity,
                    stock_after=stock_after,
                    reason="order_reserved",
                )
            )

        order.total_amount = total
        db.add(
            OutboxEvent(
                event_type="order.created",
                aggregate_id=order.id,
                payload={"order_number": order.order_number, "customer_email": customer_email},
            )
        )
        db.commit()
        db.refresh(order)
        return order, False
    except IntegrityError as exc:
        db.rollback()
        # If a concurrent request won the unique-key race, return it only if the payload matches.
        existing = db.scalar(select(Order).where(Order.idempotency_key == idempotency_key).options(selectinload(Order.items)))
        if existing:
            if existing.request_hash != request_hash:
                raise IdempotencyConflict("This idempotency key was already used for a different request.") from exc
            return existing, True
        raise
    except Exception:
        db.rollback()
        raise


def cancel_order(db: Session, order_id: int) -> Order:
    """Cancel a cancellable order and restock exactly once."""
    order = db.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
    if order is None:
        raise OrderNotFound("Order not found.")
    if order.status == OrderStatus.CANCELLED:
        return order
    if order.status not in (OrderStatus.PENDING, OrderStatus.PROCESSING):
        raise OrderStateConflict("Only pending or processing orders can be cancelled.")

    try:
        changed = db.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_([OrderStatus.PENDING, OrderStatus.PROCESSING]))
            .values(status=OrderStatus.CANCELLED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            db.rollback()
            latest = db.scalar(select(Order).where(Order.id == order_id))
            if latest and latest.status == OrderStatus.CANCELLED:
                return latest
            raise OrderStateConflict("Order status changed; refresh the page and try again.")

        for line in order.items:
            stock_after = db.execute(
                update(Product)
                .where(Product.id == line.product_id)
                .values(stock=Product.stock + line.quantity, updated_at=utcnow())
                .returning(Product.stock)
                .execution_options(synchronize_session=False)
            ).scalar_one()
            db.add(
                InventoryMovement(
                    product_id=line.product_id,
                    order_id=order.id,
                    quantity_delta=line.quantity,
                    stock_after=stock_after,
                    reason="order_cancelled",
                )
            )
        db.add(
            OutboxEvent(
                event_type="order.cancelled",
                aggregate_id=order.id,
                payload={"order_number": order.order_number},
            )
        )
        db.commit()
        db.expire(order)
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        raise


def ship_order(db: Session, order_id: int) -> Order:
    """Move a processing order to shipped exactly once."""
    order = db.scalar(select(Order).where(Order.id == order_id))
    if order is None:
        raise OrderNotFound("Order not found.")
    if order.status == OrderStatus.SHIPPED:
        return order
    if order.status != OrderStatus.PROCESSING:
        raise OrderStateConflict("Only processing orders can be marked as shipped. Run the outbox worker first.")
    try:
        changed = db.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PROCESSING)
            .values(status=OrderStatus.SHIPPED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            db.rollback()
            latest = db.scalar(select(Order).where(Order.id == order_id))
            if latest and latest.status == OrderStatus.SHIPPED:
                return latest
            raise OrderStateConflict("Order status changed; refresh the page and try again.")
        db.add(OutboxEvent(event_type="order.shipped", aggregate_id=order.id,
                           payload={"order_number": order.order_number}))
        db.commit()
        db.expire(order)
        db.refresh(order)
        return order
    except Exception:
        db.rollback()
        raise
