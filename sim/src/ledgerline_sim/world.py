"""Orchestrates one deterministic simulation run."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from faker import Faker

from .answer_key import build_answer_key, doc_status
from .bank import BankBook, FxRates
from .company import build_company, build_customers, build_vendor_master, build_vendors
from .config import SimConfig
from .emails import build_emails
from .invoices import IdSource, generate_ap, generate_ar
from .models import (
    BankTxn,
    Company,
    Customer,
    Email,
    Invoice,
    Match,
    UsageRow,
    Vendor,
    VendorRecord,
)
from .plant import DUPLICATE_PLAN, N_APPLIED_CREDIT_NOTES, N_NEAR_DUPLICATE_NEGATIVES, Planter
from .usage import build_usage

AP_SHARE = 0.74


@dataclass
class World:
    cfg: SimConfig
    company: Company
    vendors: list[Vendor]
    records: list[VendorRecord]
    customers: list[Customer]
    invoices: list[Invoice]
    txns: list[BankTxn]
    matches: list[Match]
    emails: list[Email]
    usage: list[UsageRow]
    answer_key: dict

    @property
    def ap(self) -> list[Invoice]:
        return [i for i in self.invoices if i.direction == "AP"]

    @property
    def ar(self) -> list[Invoice]:
        return [i for i in self.invoices if i.direction == "AR"]

    def closing_balance(self) -> Decimal:
        return self.company.opening_balance + sum((t.amount for t in self.txns), Decimal("0"))


def generate(cfg: SimConfig) -> World:
    rng = np.random.default_rng(cfg.seed)
    fake = Faker("fr_FR")
    fake.seed_instance(cfg.seed)

    company = build_company(rng, fake, cfg.opening_balance)
    vendors = build_vendors(rng, fake)
    records = build_vendor_master(vendors, cfg.start, rng)
    customers = build_customers(rng, fake)
    ids = IdSource()

    planter = Planter(cfg, rng, ids, company, vendors, records)
    creep = planter.price_creep_rules()
    planter.vendor_clusters()

    fixed = sum(cfg.months if v.cadence == "monthly" else math.ceil(cfg.months / 3)
                for v in vendors if v.cadence != "variable")
    planted = len(DUPLICATE_PLAN) + N_APPLIED_CREDIT_NOTES + N_NEAR_DUPLICATE_NEGATIVES + 3
    n_variable = round(cfg.n_invoices * AP_SHARE) - fixed - planted
    ap = generate_ap(cfg, rng, ids, company, vendors, planter.main_record, n_variable, creep)
    ap = planter.plant_all(ap)
    ar = generate_ar(cfg, rng, ids, company, customers, cfg.n_invoices - len(ap))

    vendor_by_id = {v.id: v for v in vendors}
    book = BankBook(cfg, rng, company)
    book.pay_suppliers(ap, vendor_by_id, FxRates(cfg, rng))
    book.collect_customers(ar, {c.id: c for c in customers})
    book.operating_flows(ap, ar)

    invoices = _assign_doc_ids(ap + ar, book.matches, rng)
    book.finalize()

    emails = build_emails(cfg, rng, company, vendor_by_id, planter.planned_emails, ap)
    emails.sort(key=lambda e: e.sent_at)
    for i, e in enumerate(emails, start=1):
        e.email_id = f"EML-{i:04d}"

    usage = build_usage(cfg, rng, vendors)
    world = World(cfg, company, vendors, records, customers, invoices, book.txns, book.matches, emails,
                  usage, answer_key={})
    world.answer_key = build_answer_key(world, planter.meta)
    _check(world)
    return world


def _assign_doc_ids(invoices: list[Invoice], matches: list[Match], rng: np.random.Generator) -> list[Invoice]:
    """Final ids follow issue date with a random tie-break, so planted docs don't stand out."""
    tiebreak = rng.random(len(invoices))
    order = sorted(range(len(invoices)), key=lambda i: (invoices[i].issue_date, tiebreak[i]))
    mapping: dict[str, str] = {}
    counters = {"AP": 0, "AR": 0}
    out = []
    for i in order:
        inv = invoices[i]
        counters[inv.direction] += 1
        mapping[inv.doc_id] = f"{inv.direction}-{counters[inv.direction]:05d}"
        out.append(inv)
    for inv in out:
        inv.doc_id = mapping[inv.doc_id]
        if inv.references_invoice:
            inv.references_invoice = mapping[inv.references_invoice]
    for m in matches:
        m.doc_id = mapping[m.doc_id]
    return out


def _check(w: World) -> None:
    """Internal consistency: fails loudly rather than shipping a broken benchmark."""
    by_txn: dict[str, Decimal] = {}
    for m in w.matches:
        by_txn[m.txn_id] = by_txn.get(m.txn_id, Decimal("0")) + m.allocated_amount
    for t in w.txns:
        if t.txn_id in by_txn and by_txn[t.txn_id] != abs(t.amount):
            raise AssertionError(f"{t.txn_id}: allocations {by_txn[t.txn_id]} != {abs(t.amount)}")
    status = doc_status(w)
    for inv in w.invoices:
        if any(tag.startswith("fraud:") for tag in inv.tags) and status[inv.doc_id][0] != "open":
            raise AssertionError(f"fraud invoice {inv.doc_id} was paid")
