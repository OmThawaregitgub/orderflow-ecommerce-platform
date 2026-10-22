"""Inventory administration use cases."""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import InventoryMovement, Product, utcnow


class InventoryError(Exception):
    pass


def restock_product(db: Session, product_id: int, quantity: int) -> Product:
    if quantity < 1 or quantity > 1_000_000:
        raise InventoryError("Restock quantity must be between 1 and 1,000,000.")
    try:
        stock_after = db.execute(
            update(Product).where(Product.id == product_id).values(stock=Product.stock + quantity, updated_at=utcnow())
            .returning(Product.stock).execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if stock_after is None:
            db.rollback()
            raise InventoryError("Product not found.")
        db.add(
            InventoryMovement(
                product_id=product_id,
                quantity_delta=quantity,
                stock_after=stock_after,
                reason="manual_restock",
            )
        )
        db.commit()
        return db.scalar(select(Product).where(Product.id == product_id))
    except Exception:
        db.rollback()
        raise
