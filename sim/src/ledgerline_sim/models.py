"""Domain objects for the synthetic company. All money is Decimal, quantized to cents."""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field

CENT = Decimal("0.01")


def money(x: float | int | str | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


DocFormat = Literal["facturx", "ubl", "cii", "pdf", "scan"]
Direction = Literal["AP", "AR"]
DocType = Literal["invoice", "credit_note"]
PayMethod = Literal["transfer", "direct_debit", "card"]
Size = Literal["micro", "sme", "eti", "large"]


class Address(BaseModel):
    street: str
    postcode: str
    city: str
    country: str = "FR"


class Product(BaseModel):
    name: str
    unit_price: Decimal
    vat_rate: Decimal


class Party(BaseModel):
    id: str
    name: str
    siren: str | None = None
    vat_number: str | None = None
    iban: str
    bic: str
    phone: str
    email_domain: str
    address: Address


class Company(Party):
    """The SME whose books we simulate."""

    opening_balance: Decimal
    bank_account_iban: str


class Vendor(Party):
    """A true supplier entity. `records` lists the vendor-master rows that point at it."""

    category: str
    size: Size
    currency: str = "EUR"
    payment_terms_days: int
    pay_method: PayMethod
    number_pattern: str
    next_seq: int
    template: int
    products: list[Product]
    cadence: Literal["monthly", "quarterly", "variable"]
    weight: float = 0.0  # share of variable invoice volume
    seats: int = 0  # SaaS seats billed
    issue_day: int = 0  # day offset within the month for recurring invoices
    created_on: date | None = None  # set for vendors created during the period


class VendorRecord(BaseModel):
    """A row in the ERP vendor master. Several records can point at one Vendor."""

    record_id: str
    vendor_id: str
    name: str
    siren: str | None
    vat_number: str | None
    iban: str
    phone: str
    email: str
    address: Address
    created_on: date


class Customer(Party):
    payment_terms_days: int
    mean_delay_days: float
    sd_delay_days: float
    pays_by_statement: bool = False
    weight: float = 1.0


class InvoiceLine(BaseModel):
    line_no: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal
    amount: Decimal  # net, = quantity * unit_price


class Invoice(BaseModel):
    doc_id: str
    direction: Direction
    doc_type: DocType = "invoice"
    number: str
    issue_date: date
    due_date: date
    currency: str = "EUR"
    seller_name: str
    seller_siren: str | None
    seller_vat: str | None
    seller_address: Address
    buyer_name: str
    buyer_siren: str | None
    buyer_vat: str | None
    buyer_address: Address
    iban: str
    bic: str
    lines: list[InvoiceLine]
    net: Decimal
    vat: Decimal
    total: Decimal
    vendor_id: str | None = None  # true entity (AP)
    vendor_record_id: str | None = None  # vendor-master row it was booked under (AP)
    customer_id: str | None = None  # AR
    format: DocFormat = "pdf"
    channel: Literal["email", "portal", "post", "pdp"] = "email"
    template: int = 0
    references_invoice: str | None = None  # credit notes: the doc_id they correct
    references_number: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)  # planted-issue tags, never rendered
    payable: bool = True  # False = must not be paid (fraud, held duplicates)

    @property
    def signed_total(self) -> Decimal:
        return -self.total if self.doc_type == "credit_note" else self.total


class BankTxn(BaseModel):
    txn_id: str
    booking_date: date
    value_date: date
    amount: Decimal  # signed, account currency (EUR)
    currency: str = "EUR"
    counterparty_name: str
    counterparty_iban: str | None
    reference: str
    bank_code: Literal["VIR", "PRLV", "CB", "FRAIS", "ECH", "VIR_RECU"]
    original_amount: Decimal | None = None  # card payments in foreign currency
    original_currency: str | None = None
    category: str = "other"  # ground truth only, never exported to the statement


class Match(BaseModel):
    """Ground-truth link between a bank transaction and an invoice."""

    txn_id: str
    doc_id: str
    allocated_amount: Decimal  # EUR, positive, portion of txn applied to this doc


class Email(BaseModel):
    email_id: str
    sent_at: datetime
    from_name: str
    from_addr: str
    reply_to: str | None = None
    to_addr: str
    subject: str
    body: str
    spf: Literal["pass", "fail", "softfail", "none"] = "pass"
    dkim: Literal["pass", "fail", "none"] = "pass"
    related_vendor_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class UsageRow(BaseModel):
    month: str  # YYYY-MM
    tool: str
    vendor_id: str
    seats_paid: int
    active_users: int
