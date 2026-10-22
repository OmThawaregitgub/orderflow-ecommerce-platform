"""Create a local demo operator and representative inventory."""
from __future__ import annotations

import argparse
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.database import build_engine, build_session_factory
from app.models import Base, Product, User
from app.security import hash_password

SAMPLE_PRODUCTS = [
    ("AUDIO-001", "Studio Wireless Headphones", "Electronics", "Over-ear wireless headphones with 30-hour battery life.", "129.00", 24, 5),
    ("HOME-014", "Ceramic Pour-over Set", "Home & Kitchen", "Minimal ceramic dripper and matching server.", "48.50", 8, 5),
    ("OFFC-002", "Everyday Desk Lamp", "Office", "Dimmable LED lamp with warm and cool light modes.", "64.00", 3, 5),
    ("GEAR-105", "Travel Organizer Pack", "Accessories", "Set of three lightweight travel organizers.", "32.00", 42, 10),
    ("AUDIO-023", "USB-C Studio Microphone", "Electronics", "Compact USB-C microphone for calls and streaming.", "89.00", 5, 5),
    ("HOME-061", "Insulated Bottle 750ml", "Home & Kitchen", "Double-wall stainless steel water bottle.", "27.50", 0, 8),
    ("OFFC-033", "Mechanical Keyboard", "Office", "Tactile keyboard with hot-swappable switches.", "109.00", 17, 5),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed OrderFlow demo data")
    parser.add_argument("--email", default="admin@orderflow.local")
    parser.add_argument("--password", default="OrderFlow123!", help="demo-only initial password")
    args = parser.parse_args()
    if settings.environment == "production":
        raise SystemExit("Refusing to seed a demo operator in production.")
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)
    factory = build_session_factory(engine)
    db = factory()
    try:
        user = db.scalar(select(User).where(User.email == args.email.lower()))
        if user is None:
            db.add(User(email=args.email.lower(), password_hash=hash_password(args.password), role="admin", is_active=True))
            print(f"Created demo operator: {args.email}")
        else:
            print(f"Demo operator already exists: {args.email}")
        for sku, name, category, description, price, stock, low in SAMPLE_PRODUCTS:
            existing = db.scalar(select(Product).where(Product.sku == sku))
            if existing is None:
                db.add(Product(sku=sku, name=name, category=category, description=description,
                               price=Decimal(price), stock=stock, low_stock_threshold=low, is_active=True))
        db.commit()
        print("Sample inventory is ready. Change or replace demo credentials before exposing this app to a network.")
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
