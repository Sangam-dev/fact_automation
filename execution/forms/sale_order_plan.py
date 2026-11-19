"""THE EDITABLE PART: what the Sale Order filler does, in order.

Change business behaviour here and nowhere else:

    add a header field     -> add  Field("payload_key")  to HEADER
    remove a header field  -> delete its Field(...) line
    change fill order      -> move the line
    add a row column       -> add  LineColumn("payload_key", "Grid Column Name")
    add a read-back check  -> add the grid column name to READBACK
    change a lookup proof  -> set  Field("key", proof="derived_field_key")

Keys on the left are payload keys from config/forms/test_payload.json and also
the keys in the compiled form map (config/forms/sale_order.json).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """One header value to type.

    ``proof`` names the read-only field the application fills once the lookup
    resolved (e.g. customer_code -> customer_name); it is read back as evidence,
    never typed.
    """

    key: str
    proof: str | None = None
    required: bool = True


@dataclass(frozen=True)
class LineColumn:
    """One grid cell to type, addressed by payload key + grid column name."""

    key: str
    column: str


@dataclass(frozen=True)
class Plan:
    name: str
    header: tuple[Field, ...]
    #: Order matters: it is the grid's tab order, and the grid refuses to move
    #: forward until product code and description are present.
    columns: tuple[LineColumn, ...]
    #: Cells read back after each line, as evidence.
    readback: tuple[str, ...]


SALE_ORDER = Plan(
    name="sale_order",
    header=(
        Field("document_scheme"),
        Field("order_date"),
        Field("proforma_no"),
        Field("customer_order_no"),
        Field("expected_delivery_date"),
        Field("currency_code", proof="currency_name"),
        Field("customer_code", proof="customer_name"),
        Field("salesman_code", proof="salesman_name"),
        Field("document_class", proof="document_class_description"),
    ),
    columns=(
        LineColumn("product_code", "Product Code"),
        LineColumn("product_description", "Product Description"),
        LineColumn("quantity", "Quantity"),
        LineColumn("rate", "Rate"),
        LineColumn("document_class", "Document Class"),
    ),
    readback=("Product Description", "Quantity", "Rate", "Amount"),
)
