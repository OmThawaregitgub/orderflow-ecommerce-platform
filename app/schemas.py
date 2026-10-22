"""Validated API request and response shapes."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderLineIn(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100)


class OrderCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_email: str = Field(min_length=3, max_length=320)
    items: list[OrderLineIn] = Field(min_length=1, max_length=50)

    @field_validator("customer_email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        import re

        value = value.lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid customer email address.")
        return value

    @model_validator(mode="after")
    def quantity_for_each_product_is_bounded(self) -> "OrderCreateIn":
        totals: dict[int, int] = {}
        for item in self.items:
            totals[item.product_id] = totals.get(item.product_id, 0) + item.quantity
        if any(quantity > 100 for quantity in totals.values()):
            raise ValueError("The maximum quantity per product per order is 100.")
        return self


class ProductCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    sku: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(default="", max_length=2000)
    category: str = Field(default="General", min_length=2, max_length=80)
    price: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
    stock: int = Field(ge=0, le=1_000_000)
    low_stock_threshold: int = Field(default=5, ge=0, le=1_000_000)


class OrderCreatedOut(BaseModel):
    order_number: str
    status: str
    total_amount: Decimal
    customer_email: str
    created_at: str
    replayed: bool = False
