from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import InventoryMovement, Order, OrderStatus, OutboxEvent, OutboxStatus, Product
from app.schemas import OrderCreateIn, OrderLineIn
from app.services.orders import IdempotencyConflict, InsufficientStock, OrderStateConflict, cancel_order, create_order, ship_order


def make_payload(product_id: int, quantity: int = 2, email: str = "buyer@example.test") -> OrderCreateIn:
    return OrderCreateIn(customer_email=email, items=[OrderLineIn(product_id=product_id, quantity=quantity)])


def test_order_reserves_stock_and_writes_outbox_atomically(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        order, replayed = create_order(db, make_payload(seed["product_id"], 3), "idempotency-create-001")
        product = db.get(Product, seed["product_id"])
        event_count = db.scalar(select(func.count(OutboxEvent.id)).where(OutboxEvent.aggregate_id == order.id))
        movement = db.scalar(select(InventoryMovement).where(InventoryMovement.order_id == order.id))
        assert not replayed
        assert order.total_amount == Decimal("37.50")
        assert product.stock == 7
        assert event_count == 1
        assert movement.quantity_delta == -3
        assert movement.stock_after == 7
        assert order.status == OrderStatus.PENDING
    finally:
        db.close()


def test_idempotent_replay_does_not_reserve_stock_twice(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        payload = make_payload(seed["product_id"], 4)
        first, first_replayed = create_order(db, payload, "retry-safe-001")
        second, second_replayed = create_order(db, payload, "retry-safe-001")
        product = db.get(Product, seed["product_id"])
        assert not first_replayed and second_replayed
        assert first.id == second.id
        assert product.stock == 6
        assert db.scalar(select(func.count(Order.id))) == 1
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1
    finally:
        db.close()


def test_reusing_idempotency_key_for_different_body_conflicts(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        create_order(db, make_payload(seed["product_id"], 1), "reused-key-001")
        with pytest.raises(IdempotencyConflict):
            create_order(db, make_payload(seed["product_id"], 2), "reused-key-001")
        assert db.get(Product, seed["product_id"]).stock == 9
    finally:
        db.close()


def test_insufficient_stock_rolls_back_every_write(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        with pytest.raises(InsufficientStock):
            create_order(db, make_payload(seed["product_id"], 11), "not-enough-001")
        assert db.get(Product, seed["product_id"]).stock == 10
        assert db.scalar(select(func.count(Order.id))) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(select(func.count(OutboxEvent.id))) == 0
    finally:
        db.close()


def test_cancellation_restocks_once_and_cannot_be_repeated(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        order, _ = create_order(db, make_payload(seed["product_id"], 3), "cancel-order-001")
        cancelled = cancel_order(db, order.id)
        again = cancel_order(db, order.id)
        product = db.get(Product, seed["product_id"])
        movements = db.scalar(select(func.count(InventoryMovement.id)).where(InventoryMovement.order_id == order.id))
        assert cancelled.status == OrderStatus.CANCELLED
        assert again.status == OrderStatus.CANCELLED
        assert product.stock == 10
        assert movements == 2  # one reservation and one return; the second cancel adds nothing
    finally:
        db.close()


def test_ship_transition_requires_processing_and_is_idempotent(app_client):
    _, application, seed = app_client
    db: Session = application.state.session_factory()
    try:
        order, _ = create_order(db, make_payload(seed["product_id"], 1), "ship-order-001")
        with pytest.raises(OrderStateConflict):
            ship_order(db, order.id)
        order.status = OrderStatus.PROCESSING
        db.commit()
        shipped = ship_order(db, order.id)
        again = ship_order(db, order.id)
        event_count = db.scalar(select(func.count(OutboxEvent.id)).where(
            OutboxEvent.aggregate_id == order.id, OutboxEvent.event_type == "order.shipped"))
        assert shipped.status == OrderStatus.SHIPPED
        assert again.status == OrderStatus.SHIPPED
        assert event_count == 1
    finally:
        db.close()
