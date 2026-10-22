"""Server-rendered operations console routes."""
from __future__ import annotations

import secrets
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.templating import Jinja2Templates

from app.database import get_db
from app.models import InventoryMovement, Order, OrderStatus, Product, User
from app.schemas import OrderCreateIn, OrderLineIn, ProductCreateIn
from app.security import csrf_is_valid, hash_password, new_csrf_token, verify_password
from app.services.analytics import dashboard_metrics
from app.services.inventory import InventoryError, restock_product
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

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))


def csrf_token(request: Request) -> str:
    token = request.session.get("_csrf")
    if not token:
        token = new_csrf_token()
        request.session["_csrf"] = token
    return token


def check_csrf(request: Request, supplied: str) -> bool:
    return csrf_is_valid(request.session.get("_csrf"), supplied)


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session["_flash"] = {"message": message, "kind": kind}


def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    current = datetime.now(timezone.utc)
    merged = {"request": request, "csrf_token": csrf_token(request), "user": None, "flash": request.session.pop("_flash", None),
              "today_label": current.strftime("%A, %B %d"), "now_year": current.year}
    merged.update(context)
    return templates.TemplateResponse(request=request, name=name, context=merged, status_code=status_code)


def authenticated_user(request: Request, db: Session) -> User | RedirectResponse:
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", status_code=303)
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return user


def money(value: Decimal | int | float | None) -> str:
    return f"${Decimal(value or 0):,.2f}"


templates.env.filters["money"] = money
templates.env.filters["datetime"] = lambda value: value.strftime("%b %d, %Y · %I:%M %p") if value else "—"


@router.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse("/dashboard" if request.session.get("user_id") else "/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id") and db.get(User, int(request.session["user_id"])):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", {"page_title": "Sign in"})


@router.post("/login", response_class=HTMLResponse, include_in_schema=False)
def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str, Form(alias="csrf_token")],
    db: Session = Depends(get_db),
):
    if not check_csrf(request, csrf):
        return render(request, "login.html", {"page_title": "Sign in", "error": "Your form expired. Refresh the page and try again."}, 403)
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"page_title": "Sign in", "error": "Email or password is incorrect."}, 401)
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["_csrf"] = new_csrf_token()
    flash(request, "Welcome back. Your workspace is ready.")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout", include_in_schema=False)
def logout(request: Request, csrf: Annotated[str, Form(alias="csrf_token")]):
    if not check_csrf(request, csrf):
        return HTMLResponse("Invalid CSRF token", status_code=403)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    metrics = dashboard_metrics(db)
    recent_orders = db.scalars(select(Order).options(selectinload(Order.items)).order_by(Order.created_at.desc()).limit(7)).all()
    low_stock_products = db.scalars(
        select(Product).where(Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold)
        .order_by(Product.stock.asc(), Product.name.asc()).limit(5)
    ).all()
    return render(request, "dashboard.html", {
        "page_title": "Overview", "active_nav": "dashboard", "user": user, "metrics": metrics,
        "recent_orders": recent_orders, "low_stock_products": low_stock_products,
        "max_chart_count": max((d["count"] for d in metrics["daily"]), default=0),
    })


@router.get("/products", response_class=HTMLResponse, include_in_schema=False)
def products_page(
    request: Request,
    q: str = Query(default="", max_length=120),
    state: str = Query(default="active", pattern="^(active|all|inactive)$"),
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    stmt = select(Product)
    if state == "active":
        stmt = stmt.where(Product.is_active.is_(True))
    elif state == "inactive":
        stmt = stmt.where(Product.is_active.is_(False))
    if q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(term), Product.sku.ilike(term), Product.category.ilike(term)))
    items = db.scalars(stmt.order_by(Product.is_active.desc(), Product.name.asc())).all()
    inventory_movements = db.execute(
        select(InventoryMovement, Product).join(Product, Product.id == InventoryMovement.product_id)
        .order_by(InventoryMovement.created_at.desc()).limit(8)
    ).all()
    total_active = db.scalar(select(func.count(Product.id)).where(Product.is_active.is_(True))) or 0
    low_count = db.scalar(select(func.count(Product.id)).where(Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold)) or 0
    return render(request, "products.html", {
        "page_title": "Inventory", "active_nav": "products", "user": user, "products": items,
        "query": q, "state": state, "total_active": total_active, "low_count": low_count,
        "inventory_movements": inventory_movements,
    })


@router.post("/products/new", include_in_schema=False)
def create_product(
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    sku: Annotated[str, Form()],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "General",
    price: Annotated[str, Form()] = "0",
    stock: Annotated[int, Form()] = 0,
    low_stock_threshold: Annotated[int, Form()] = 5,
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse("/products", status_code=303)
    try:
        data = ProductCreateIn(sku=sku.strip().upper(), name=name, description=description, category=category,
                               price=Decimal(price), stock=stock, low_stock_threshold=low_stock_threshold)
    except (ValidationError, ValueError):
        flash(request, "Check the product fields. SKU, name, positive price and non-negative stock are required.", "error")
        return RedirectResponse("/products", status_code=303)
    try:
        db.add(Product(sku=data.sku, name=data.name, description=data.description, category=data.category,
                       price=data.price, stock=data.stock, low_stock_threshold=data.low_stock_threshold, is_active=True))
        db.commit()
        flash(request, f"{data.name} was added to inventory.")
    except IntegrityError:
        db.rollback()
        flash(request, f"SKU {data.sku} already exists. Choose a unique SKU.", "error")
    return RedirectResponse("/products", status_code=303)


@router.post("/products/{product_id}/restock", include_in_schema=False)
def restock(
    product_id: int,
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    quantity: Annotated[int, Form()],
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse("/products", status_code=303)
    try:
        product = restock_product(db, product_id, quantity)
        flash(request, f"Restocked {product.name} by {quantity} unit(s).")
    except InventoryError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/products", status_code=303)


@router.post("/products/{product_id}/toggle", include_in_schema=False)
def toggle_product(
    product_id: int,
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse("/products", status_code=303)
    product = db.get(Product, product_id)
    if not product:
        flash(request, "Product not found.", "error")
    else:
        product.is_active = not product.is_active
        product.updated_at = datetime.now(timezone.utc)
        db.commit()
        flash(request, f"{product.name} is now {'active' if product.is_active else 'inactive'}.")
    return RedirectResponse("/products?state=all", status_code=303)


@router.get("/orders", response_class=HTMLResponse, include_in_schema=False)
def orders_page(
    request: Request,
    q: str = Query(default="", max_length=120),
    status: str = Query(default="all", pattern="^(all|pending|processing|shipped|cancelled)$"),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    stmt = select(Order).options(selectinload(Order.items))
    if status != "all":
        stmt = stmt.where(Order.status == OrderStatus(status))
    if q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(or_(Order.order_number.ilike(term), Order.customer_email.ilike(term)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page_size = 12
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    orders = db.scalars(stmt.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    status_counts = {s.value: db.scalar(select(func.count(Order.id)).where(Order.status == s)) or 0 for s in OrderStatus}
    return render(request, "orders.html", {
        "page_title": "Orders", "active_nav": "orders", "user": user, "orders": orders, "query": q,
        "status_filter": status, "page": page, "pages": pages, "total": total, "status_counts": status_counts,
    })


@router.get("/orders/new", response_class=HTMLResponse, include_in_schema=False)
def new_order_page(request: Request, db: Session = Depends(get_db)):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    products = db.scalars(select(Product).where(Product.is_active.is_(True), Product.stock > 0).order_by(Product.name)).all()
    return render(request, "order_new.html", {"page_title": "Create order", "active_nav": "orders", "user": user,
                                                "products": products, "idempotency_key": secrets.token_urlsafe(24)})


@router.post("/orders/create", include_in_schema=False)
def create_order_form(
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    customer_email: Annotated[str, Form()],
    product_id: Annotated[int, Form()],
    quantity: Annotated[int, Form()],
    idempotency_key: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse("/orders/new", status_code=303)
    try:
        payload = OrderCreateIn(customer_email=customer_email, items=[OrderLineIn(product_id=product_id, quantity=quantity)])
        order, replayed = create_order(db, payload, idempotency_key)
        flash(request, f"Order {order.order_number} {'was already accepted' if replayed else 'was created'} successfully.")
        return RedirectResponse(f"/orders/{order.id}", status_code=303)
    except (ValidationError, ValueError) as exc:
        flash(request, f"Please check the order fields: {str(exc)[:180]}", "error")
    except (OrderError, IntegrityError) as exc:
        flash(request, str(exc), "error")
    return RedirectResponse("/orders/new", status_code=303)


@router.post("/orders/{order_id}/ship", include_in_schema=False)
def ship_order_form(
    order_id: int,
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    try:
        order = ship_order(db, order_id)
        flash(request, f"Order {order.order_number} was marked as shipped.")
    except OrderError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse, include_in_schema=False)
def order_detail(order_id: int, request: Request, db: Session = Depends(get_db)):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    order = db.scalar(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
    if order is None:
        return render(request, "not_found.html", {"page_title": "Order not found", "user": user}, 404)
    return render(request, "order_detail.html", {"page_title": order.order_number, "active_nav": "orders", "user": user, "order": order})


@router.post("/orders/{order_id}/cancel", include_in_schema=False)
def cancel_order_form(
    order_id: int,
    request: Request,
    csrf: Annotated[str, Form(alias="csrf_token")],
    db: Session = Depends(get_db),
):
    user = authenticated_user(request, db)
    if not isinstance(user, User):
        return user
    if not check_csrf(request, csrf):
        flash(request, "Form validation failed. Please retry.", "error")
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    try:
        order = cancel_order(db, order_id)
        flash(request, f"Order {order.order_number} was cancelled; reserved inventory was returned.")
    except OrderError as exc:
        flash(request, str(exc), "error")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)
