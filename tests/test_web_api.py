from __future__ import annotations


def test_health_and_readiness(app_client):
    client, _, _ = app_client
    assert client.get("/healthz").json() == {"status": "ok", "service": "orderflow"}
    assert client.get("/readyz").status_code == 200


def test_workspace_requires_sign_in(app_client):
    client, _, _ = app_client
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_dashboard_and_inventory_render_after_sign_in(authenticated_client):
    client, _, seed, _ = authenticated_client
    dashboard = client.get("/dashboard")
    products = client.get("/products")
    assert dashboard.status_code == 200
    assert "Order activity" in dashboard.text
    assert "Inventory health" in dashboard.text
    assert products.status_code == 200
    assert "Test product" in products.text


def test_api_requires_authentication(app_client):
    client, _, _ = app_client
    response = client.get("/api/v1/products")
    assert response.status_code == 401


def test_api_order_idempotency(authenticated_client):
    client, _, seed, csrf = authenticated_client
    payload = {"customer_email": "buyer@example.test", "items": [{"product_id": seed["product_id"], "quantity": 2}]}
    headers = {"Idempotency-Key": "api-idempotency-001", "X-CSRF-Token": csrf}
    first = client.post("/api/v1/orders", json=payload, headers=headers)
    second = client.post("/api/v1/orders", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["order_number"] == second.json()["order_number"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True


def test_api_requires_idempotency_key(authenticated_client):
    client, _, seed, csrf = authenticated_client
    response = client.post("/api/v1/orders", json={"customer_email": "buyer@example.test", "items": [{"product_id": seed["product_id"], "quantity": 1}]}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 400


def test_create_order_page_and_inventory_audit_render(authenticated_client):
    client, _, _, _ = authenticated_client
    page = client.get("/orders/new")
    inventory = client.get("/products")
    assert page.status_code == 200
    assert "Reserve inventory" in page.text
    assert inventory.status_code == 200
    assert "Recent inventory activity" in inventory.text


def test_html_order_form_reserves_stock(authenticated_client):
    client, application, seed, csrf = authenticated_client
    listing = client.get("/orders")
    assert listing.status_code == 200
    assert "ORDER LIFECYCLE" in listing.text
    created = client.post("/orders/create", data={
        "csrf_token": csrf,
        "customer_email": "web-buyer@example.test",
        "product_id": seed["product_id"],
        "quantity": 2,
        "idempotency_key": "web-form-order-001",
    }, follow_redirects=True)
    assert created.status_code == 200
    assert "Order information" in created.text
    assert "web-buyer@example.test" in created.text
    db = application.state.session_factory()
    try:
        from app.models import Product
        assert db.get(Product, seed["product_id"]).stock == 8
    finally:
        db.close()


def test_product_creation_and_restock_form(authenticated_client):
    client, application, _, csrf = authenticated_client
    created = client.post("/products/new", data={
        "csrf_token": csrf,
        "sku": "WEB-NEW-01",
        "name": "Web Created Item",
        "description": "Added through the inventory dialog form",
        "category": "Test Gear",
        "price": "19.95",
        "stock": "4",
        "low_stock_threshold": "3",
    }, follow_redirects=True)
    assert created.status_code == 200
    assert "Web Created Item" in created.text
    db = application.state.session_factory()
    try:
        from sqlalchemy import select
        from app.models import InventoryMovement, Product
        product = db.scalar(select(Product).where(Product.sku == "WEB-NEW-01"))
        assert product is not None and product.stock == 4
        product_id = product.id
    finally:
        db.close()
    restocked = client.post(f"/products/{product_id}/restock", data={"csrf_token": csrf, "quantity": "3"}, follow_redirects=True)
    assert restocked.status_code == 200
    assert "Restocked Web Created Item by 3" in restocked.text
    db = application.state.session_factory()
    try:
        from sqlalchemy import select
        from app.models import InventoryMovement, Product
        product = db.get(Product, product_id)
        assert product.stock == 7
        assert db.scalar(select(InventoryMovement).where(InventoryMovement.product_id == product_id, InventoryMovement.reason == "manual_restock")) is not None
    finally:
        db.close()


def test_operator_can_mark_processed_order_shipped(authenticated_client):
    client, application, seed, csrf = authenticated_client
    from app.models import Order, OrderStatus
    from app.schemas import OrderCreateIn, OrderLineIn
    from app.services.orders import create_order
    from app.services.outbox import process_one
    from sqlalchemy import select

    db = application.state.session_factory()
    try:
        payload = OrderCreateIn(customer_email="ship-buyer@example.test", items=[OrderLineIn(product_id=seed["product_id"], quantity=1)])
        order, _ = create_order(db, payload, "web-ship-flow-001")
        order_id = order.id
        process_one(db)
    finally:
        db.close()

    response = client.post(f"/orders/{order_id}/ship", data={"csrf_token": csrf}, follow_redirects=True)
    assert response.status_code == 200
    assert "marked as shipped" in response.text.lower()
    db = application.state.session_factory()
    try:
        shipped = db.scalar(select(Order).where(Order.id == order_id))
        assert shipped.status == OrderStatus.SHIPPED
    finally:
        db.close()
