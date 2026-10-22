"""Dashboard metrics kept in database queries instead of Python-side table scans."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, OutboxEvent, OutboxStatus, Product


def dashboard_metrics(db: Session) -> dict:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_start - timedelta(days=6)
    total_orders = db.scalar(select(func.count(Order.id))) or 0
    today_orders = db.scalar(select(func.count(Order.id)).where(Order.created_at >= today_start)) or 0
    revenue = db.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(Order.status != OrderStatus.CANCELLED)
    ) or Decimal("0.00")
    pending = db.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.PENDING)) or 0
    low_stock = db.scalar(
        select(func.count(Product.id)).where(
            Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold
        )
    ) or 0
    outbox_pending = db.scalar(select(func.count(OutboxEvent.id)).where(OutboxEvent.status == OutboxStatus.PENDING)) or 0
    outbox_failed = db.scalar(select(func.count(OutboxEvent.id)).where(OutboxEvent.status == OutboxStatus.FAILED)) or 0

    daily_rows = db.execute(
        select(func.date(Order.created_at), func.count(Order.id))
        .where(Order.created_at >= since)
        .group_by(func.date(Order.created_at))
        .order_by(func.date(Order.created_at))
    ).all()
    values = {str(day): int(count) for day, count in daily_rows}
    daily = []
    max_count = max(values.values(), default=0)
    for offset in range(7):
        day = (today_start - timedelta(days=6 - offset)).date().isoformat()
        count = values.get(day, 0)
        daily.append({"label": datetime.fromisoformat(day).strftime("%a"), "date": day, "count": count,
                      "height": max(6, round((count / max_count) * 100)) if max_count else 6})

    return {
        "total_orders": total_orders,
        "today_orders": today_orders,
        "revenue": Decimal(revenue).quantize(Decimal("0.01")),
        "pending_orders": pending,
        "low_stock": low_stock,
        "outbox_pending": outbox_pending,
        "outbox_failed": outbox_failed,
        "daily": daily,
    }
