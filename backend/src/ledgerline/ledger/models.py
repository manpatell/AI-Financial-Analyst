"""The ledger: Ledgerline's single source of truth.

Conventions
- Money is NUMERIC(14,2) and Decimal in Python. Never float.
- Every row that came from a file keeps `source_file` (+ sha256 for documents), so any number can be
  traced back to the document it was read from.
- `decisions` and `calc_outputs` are append-only (enforced by DB triggers, see the migration).
  An approval is a new decision row pointing at the one it approves (`parent_id`).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(14, 2, asdecimal=True)
Qty = Numeric(14, 4, asdecimal=True)
Rate = Numeric(5, 2, asdecimal=True)
Score = Numeric(6, 5, asdecimal=True)


def _in(col: str, *values: str) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Vendor(Timestamped, Base):
    """One vendor-master row. Rows found to be the same company point at a canonical row."""

    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_record_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    canonical_vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    name: Mapped[str] = mapped_column(String(200))
    siren: Mapped[str | None] = mapped_column(String(9), index=True)
    vat_number: Mapped[str | None] = mapped_column(String(20), index=True)
    iban: Mapped[str | None] = mapped_column(String(34), index=True)
    bic: Mapped[str | None] = mapped_column(String(11))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(200))
    street: Mapped[str | None] = mapped_column(String(200))
    postcode: Mapped[str | None] = mapped_column(String(16))
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2), default="FR")
    created_on: Mapped[date | None] = mapped_column(Date)

    canonical: Mapped[Vendor | None] = relationship(remote_side="Vendor.id")


class Customer(Timestamped, Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    siren: Mapped[str | None] = mapped_column(String(9), index=True)
    vat_number: Mapped[str | None] = mapped_column(String(20))
    iban: Mapped[str | None] = mapped_column(String(34), index=True)
    street: Mapped[str | None] = mapped_column(String(200))
    postcode: Mapped[str | None] = mapped_column(String(16))
    city: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str] = mapped_column(String(2), default="FR")
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)


class Invoice(Timestamped, Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(_in("direction", "AP", "AR"), name="direction"),
        CheckConstraint(_in("doc_type", "invoice", "credit_note"), name="doc_type"),
        CheckConstraint(_in("source_format", "facturx", "ubl", "cii", "pdf", "scan"), name="source_format"),
        CheckConstraint("(direction = 'AP') = (customer_id IS NULL)", name="party_matches_direction"),
        Index("ix_invoices_vendor_number", "vendor_id", "number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(2))
    doc_type: Mapped[str] = mapped_column(String(12), default="invoice")
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    number: Mapped[str] = mapped_column(String(64))
    issue_date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    net: Mapped[Decimal] = mapped_column(Money)
    vat: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    iban_on_invoice: Mapped[str | None] = mapped_column(String(34))
    bic_on_invoice: Mapped[str | None] = mapped_column(String(11))
    seller_name: Mapped[str | None] = mapped_column(String(200))
    seller_siren: Mapped[str | None] = mapped_column(String(9))
    seller_vat: Mapped[str | None] = mapped_column(String(20))
    buyer_name: Mapped[str | None] = mapped_column(String(200))
    references_number: Mapped[str | None] = mapped_column(String(64))
    credit_of_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    # provenance of the extraction
    source_format: Mapped[str] = mapped_column(String(8))
    source_file: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    extraction_route: Mapped[str | None] = mapped_column(String(32))  # xml | small_llm | frontier_llm
    extraction_model: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # per-field confidence
    raw_fields: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    lines: Mapped[list[InvoiceLine]] = relationship(back_populates="invoice", cascade="all, delete-orphan",
                                                    order_by="InvoiceLine.line_no")
    vendor: Mapped[Vendor | None] = relationship()
    customer: Mapped[Customer | None] = relationship()


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (UniqueConstraint("invoice_id", "line_no", name="uq_invoice_lines_invoice_line"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Qty)
    unit_price: Mapped[Decimal | None] = mapped_column(Qty)
    vat_rate: Mapped[Decimal | None] = mapped_column(Rate)
    amount: Mapped[Decimal] = mapped_column(Money)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class BankTransaction(Timestamped, Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bank_ref: Mapped[str] = mapped_column(String(64), unique=True)
    account_iban: Mapped[str] = mapped_column(String(34))
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    value_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)  # signed: negative = money out
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    original_amount: Mapped[Decimal | None] = mapped_column(Money)
    original_currency: Mapped[str | None] = mapped_column(String(3))
    counterparty_name: Mapped[str | None] = mapped_column(String(200))
    counterparty_iban: Mapped[str | None] = mapped_column(String(34), index=True)
    reference: Mapped[str | None] = mapped_column(Text)
    bank_code: Mapped[str | None] = mapped_column(String(16))
    source_file: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Email(Timestamped, Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    from_name: Mapped[str | None] = mapped_column(String(200))
    from_addr: Mapped[str] = mapped_column(String(255))
    reply_to: Mapped[str | None] = mapped_column(String(255))
    to_addr: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    auth_results: Mapped[str | None] = mapped_column(Text)
    risk_signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"))
    source_file: Mapped[str] = mapped_column(Text)


class Decision(Base):
    """Append-only audit log. Every automated or human action writes one row; nothing is edited."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))
    actor: Mapped[str] = mapped_column(String(100))  # "agent" | "rule-engine" | "user:<name>"
    action: Mapped[str] = mapped_column(String(64))  # e.g. match.auto_accept, payment.hold, approve
    target: Mapped[dict[str, Any]] = mapped_column(JSONB)  # {"invoice_ids": [...], "txn_ids": [...]}
    rule_applied: Mapped[str | None] = mapped_column(String(200))
    model_version: Mapped[str | None] = mapped_column(String(100))
    input_lineage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    confidence: Mapped[Decimal | None] = mapped_column(Score)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Match(Timestamped, Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(_in("method", "rule", "ml", "llm", "human"), name="method"),
        CheckConstraint(_in("status", "proposed", "accepted", "rejected"), name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txn_id: Mapped[int] = mapped_column(ForeignKey("bank_transactions.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    allocated_amount: Mapped[Decimal] = mapped_column(Money)  # signed per document (credit notes < 0)
    method: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[Decimal | None] = mapped_column(Score)
    status: Mapped[str] = mapped_column(String(10), default="proposed")
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"))


class Finding(Timestamped, Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(_in("kind", "duplicate", "fraud", "price_creep", "unused_subscription",
                            "vendor_merge", "late_payer"), name="kind"),
        CheckConstraint(_in("status", "open", "confirmed", "dismissed", "resolved"), name="status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    subject: Mapped[dict[str, Any]] = mapped_column(JSONB)  # ids involved, e.g. {"invoice_ids": [1, 2]}
    amount_at_risk: Mapped[Decimal | None] = mapped_column(Money)
    score: Mapped[Decimal | None] = mapped_column(Score)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="open")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class CalcOutput(Base):
    """Stored tool results; `calc:<id>` provenance ids point here. Append-only."""

    __tablename__ = "calc_outputs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # content hash prefix
    tool: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
