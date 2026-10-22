"""Versioned JSON API. Mutations are session-authenticated and CSRF protected."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import Order, Product, User
from app.schemas import OrderCreateIn, ProductCreateIn
from app.security import csrf_is_valid
from app.services.orders import (
    IdempotencyConflict,
    InsufficientStock,
    OrderError,
    OrderNotFound,
    OrderStateConflict,
    ProductUnavailable,
    cancel_order,
    create_order,
    ship_order,
)

router = APIRouter(prefix="/api/v1", tags=["API v1"])


def api_user(request: Request, db: Session) -> User:
    user_id = request.session.get("user_id")
    user = db.get(User, int(user_id)) if user_id else None
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to use this endpoint.")
    return user


def verify_api_csrf(request: Request, token: str | None) -> None:
    if not csrf_is_valid(request.session.get("_csrf"), token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def serialize_order(order: Order, replayed: bool = False) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "customer_email": order.customer_email,
        "status": order.status.value,
        "total_amount": str(order.total_amount),
        "created_at": order.created_at.isoformat(),
        "replayed": replayed,
        "items": [
            {"product_id": item.product_id, "sku": item.sku_snapshot, "name": item.product_name_snapshot,
             "quantity": item.quantity, "unit_price": str(item.unit_price), "line_total": str(item.line_total)}
            for item in order.items
        ],
    }


@router.get("/products")
def list_products(request: Request, db: Session = Depends(get_db)):
    api_user(request, db)
    products = db.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.name)).all()
    return [{"id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
             "price": str(p.price), "stock": p.stock} for p in products]


@router.post("/orders", status_code=201)
def post_order(
    payload: OrderCreateIn,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    api_user(request, db)
    verify_api_csrf(request, x_csrf_token)
    try:
        order, replayed = create_order(db, payload, idempotency_key or "")
        return serialize_order(order, replayed)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InsufficientStock as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/orders/{order_number}")
def get_order(order_number: str, request: Request, db: Session = Depends(get_db)):
    api_user(request, db)
    order = db.scalar(select(Order).where(Order.order_number == order_number).options(selectinload(Order.items)))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    return serialize_order(order)


@router.post("/orders/{order_number}/cancel")
def post_cancel(
    order_number: str,
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    api_user(request, db)
    verify_api_csrf(request, x_csrf_token)
    order = db.scalar(select(Order.id).where(Order.order_number == order_number))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    try:
        return serialize_order(cancel_order(db, order))
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrderStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/orders/{order_number}/ship")
def post_ship(
    order_number: str,
    request: Request,
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    api_user(request, db)
    verify_api_csrf(request, x_csrf_token)
    order_id = db.scalar(select(Order.id).where(Order.order_number == order_number))
    if not order_id:
        raise HTTPException(status_code=404, detail="Order not found.")
    try:
        return serialize_order(ship_order(db, order_id))
    except OrderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OrderStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
