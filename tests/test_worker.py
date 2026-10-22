from sqlalchemy import select

from app.models import Order, OrderStatus, OutboxEvent, OutboxStatus
from app.schemas import OrderCreateIn, OrderLineIn
from app.services.orders import create_order
from app.services.outbox import process_one


def test_worker_consumes_outbox_and_advances_order(app_client):
    _, application, seed = app_client
    db = application.state.session_factory()
    try:
        payload = OrderCreateIn(customer_email="buyer@example.test", items=[OrderLineIn(product_id=seed["product_id"], quantity=1)])
        order, _ = create_order(db, payload, "worker-event-001")
        assert order.status == OrderStatus.PENDING
        assert process_one(db) is True
        db.expire_all()
        refreshed = db.scalar(select(Order).where(Order.id == order.id))
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == order.id))
        assert refreshed.status == OrderStatus.PROCESSING
        assert event.status == OutboxStatus.PROCESSED
        assert event.attempts == 1
        assert process_one(db) is False
    finally:
        db.close()
