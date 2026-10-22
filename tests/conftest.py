from __future__ import annotations

import re
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Product, User
from app.security import hash_password


@pytest.fixture
def app_client(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'test_orderflow.db'}"
    application = create_app(database_url)
    with TestClient(application) as client:
        factory = application.state.session_factory
        db = factory()
        user = User(email="operator@example.test", password_hash=hash_password("CorrectHorseBattery!"), role="admin", is_active=True)
        product = Product(sku="TEST-001", name="Test product", category="Testing", description="Test fixture",
                          price=Decimal("12.50"), stock=10, low_stock_threshold=2, is_active=True)
        db.add_all([user, product])
        db.commit()
        db.refresh(user)
        db.refresh(product)
        seed = {"user_id": user.id, "product_id": product.id}
        db.close()
        yield client, application, seed
    application.state.engine.dispose()


def login(client: TestClient) -> str:
    page = client.get("/login")
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token_match, "Login page should contain a CSRF token"
    response = client.post("/login", data={
        "csrf_token": token_match.group(1),
        "email": "operator@example.test",
        "password": "CorrectHorseBattery!",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "Good to see you again" in response.text
    dashboard_token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert dashboard_token
    return dashboard_token.group(1)


@pytest.fixture
def authenticated_client(app_client):
    client, application, seed = app_client
    token = login(client)
    return client, application, seed, token
