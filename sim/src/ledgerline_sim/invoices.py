"""AP and AR invoice generation (clean data; problems are planted later in plant.py)."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from . import catalog
from .company import SEASONALITY, next_business_day
from .config import EINVOICING_REFORM, SimConfig
from .models import (
    CENT,
    Company,
    Customer,
    DocFormat,
    Invoice,
    InvoiceLine,
    Party,
    Product,
    Vendor,
    VendorRecord,
    money,
)

# Typical invoice size (EUR, net) for variable-cadence vendors, by category.
TARGET_AMOUNT = {
    "materials": 2600, "logistics": 420, "packaging": 750, "services": 2300,
    "marketing": 2800, "maintenance": 650,
}


class IdSource:
    """Temporary ids; final ids are assigned in date order once everything is planted."""

    def __init__(self) -> None:
        self._n: dict[str, int] = defaultdict(int)

    def next(self, prefix: str) -> str:
        self._n[prefix] += 1
        return f"tmp-{prefix}-{self._n[prefix]}"


def format_number(pattern: str, seq: int, d: date) -> str:
    return pattern.format(
        n=seq, n3=f"{seq % 1000:03d}", n4=f"{seq:04d}", n5=f"{seq:05d}", n6=f"{seq:06d}",
        yyyy=d.year, yy=f"{d.year % 100:02d}", mm=f"{d.month:02d}",
    )


def next_number(v: Vendor, d: date) -> str:
    n = format_number(v.number_pattern, v.next_seq, d)
    v.next_seq += 1
    return n


def unit_price_q(p: Decimal) -> Decimal:
    return p.quantize(Decimal("0.0001")) if p < 1 else p.quantize(CENT)


def build_lines(items: list[tuple[str, Decimal, Decimal, Decimal]]) -> list[InvoiceLine]:
    """items: (description, quantity, unit_price, vat_rate)."""
    return [
        InvoiceLine(line_no=i, description=desc, quantity=qty, unit_price=unit_price_q(up),
                    vat_rate=rate, amount=money(qty * unit_price_q(up)))
        for i, (desc, qty, up, rate) in enumerate(items, start=1)
    ]


def totals(lines: list[InvoiceLine]) -> tuple[Decimal, Decimal, Decimal]:
    by_rate: dict[Decimal, Decimal] = defaultdict(Decimal)
    for ln in lines:
        by_rate[ln.vat_rate] += ln.amount
    net = sum((ln.amount for ln in lines), Decimal("0.00"))
    vat = sum((money(base * rate / 100) for rate, base in by_rate.items()), Decimal("0.00"))
    return net, vat, net + vat


def choose_format(v: Vendor, issue: date, rng: np.random.Generator) -> DocFormat:
    if v.address.country != "FR":
        return "pdf"
    after = issue >= EINVOICING_REFORM
    if v.category == "saas":
        table = {"facturx": 0.3, "pdf": 0.7}
    elif v.size in ("large", "eti"):
        table = ({"facturx": 0.7, "ubl": 0.2, "cii": 0.1} if after else
                 {"facturx": 0.45, "ubl": 0.15, "cii": 0.05, "pdf": 0.3, "scan": 0.05})
    elif v.size == "sme":
        table = ({"facturx": 0.3, "ubl": 0.05, "pdf": 0.45, "scan": 0.2} if after else
                 {"facturx": 0.15, "pdf": 0.55, "scan": 0.3})
    else:
        table = {"facturx": 0.1, "pdf": 0.5, "scan": 0.4} if after else {"pdf": 0.5, "scan": 0.5}
    keys = list(table)
    return keys[int(rng.choice(len(keys), p=np.array(list(table.values())) / sum(table.values())))]


def channel_for(fmt: DocFormat, issue: date) -> str:
    if fmt == "scan":
        return "post"
    if fmt in ("facturx", "ubl", "cii"):
        return "pdp" if issue >= EINVOICING_REFORM else "email"
    return "email"


def make_invoice(
    ids: IdSource, *, direction: str, seller: Party, buyer: Party, number: str, issue: date,
    terms_days: int, lines: list[InvoiceLine], currency: str = "EUR", fmt: DocFormat = "pdf",
    template: int = 0, iban: str | None = None, doc_type: str = "invoice", **extra,
) -> Invoice:
    net, vat, total = totals(lines)
    return Invoice(
        doc_id=ids.next(direction), direction=direction, doc_type=doc_type, number=number,
        issue_date=issue, due_date=issue + timedelta(days=terms_days), currency=currency,
        seller_name=seller.name, seller_siren=seller.siren, seller_vat=seller.vat_number,
        seller_address=seller.address, buyer_name=buyer.name, buyer_siren=buyer.siren,
        buyer_vat=buyer.vat_number, buyer_address=buyer.address,
        iban=iban or seller.iban, bic=seller.bic, lines=lines, net=net, vat=vat, total=total,
        format=fmt, channel=channel_for(fmt, issue), template=template, **extra,
    )


def ap_invoice(ids: IdSource, v: Vendor, record: VendorRecord, company: Company, issue: date,
               lines: list[InvoiceLine], rng: np.random.Generator, **extra) -> Invoice:
    fmt = extra.pop("fmt", None) or choose_format(v, issue, rng)
    inv = make_invoice(
        ids, direction="AP", seller=v, buyer=company, number=extra.pop("number", None)
        or next_number(v, issue), issue=issue, terms_days=v.payment_terms_days, lines=lines,
        currency=v.currency, fmt=fmt, template=v.template, vendor_id=v.id,
        vendor_record_id=record.record_id, **extra,
    )
    if v.address.country != "FR":
        inv.note = "Reverse charge - Article 196 of Council Directive 2006/112/EC"
    return inv


# ---------------------------------------------------------------- quantities


def _winter(d: date) -> float:
    return {12: 1.35, 1: 1.4, 2: 1.3, 11: 1.15, 3: 1.15, 7: 0.85, 8: 0.7}.get(d.month, 1.0)


def recurring_qty(p: Product, v: Vendor, d: date, cfg: SimConfig, rng: np.random.Generator) -> Decimal:
    n = p.name
    if "kWh" in n:
        return Decimal(int(9000 * _winter(d) * rng.uniform(0.92, 1.08)))
    if "m³" in n:
        return Decimal(int(rng.integers(140, 220)))
    if "Copies" in n:
        return Decimal(int(rng.integers(2500, 6000)))
    if "ligne" in n:
        return Decimal(14)
    if "par salarié" in n:
        return Decimal(cfg.headcount(d))
    if v.seats:
        return Decimal(v.seats)
    return Decimal(1)


def creep_factor(creep: dict, v: Vendor, p: Product, months_in: int) -> Decimal:
    rule = creep.get(v.id)
    if not rule or (rule["products"] and p.name not in rule["products"]):
        return Decimal(1)
    return Decimal(str((1 + rule["monthly_rate"]) ** months_in))


# ---------------------------------------------------------------- AP


def generate_ap(
    cfg: SimConfig, rng: np.random.Generator, ids: IdSource, company: Company,
    vendors: list[Vendor], records: dict[str, VendorRecord], n_variable: int, creep: dict,
) -> list[Invoice]:
    out: list[Invoice] = []
    months = cfg.month_starts
    for v in vendors:
        rec = records[v.id]
        if v.cadence in ("monthly", "quarterly"):
            offset = int(rng.integers(3))
            for mi, ms in enumerate(months):
                if v.cadence == "quarterly" and (mi - offset) % 3:
                    continue
                issue = next_business_day(ms + timedelta(days=v.issue_day))
                items = [(p.name, recurring_qty(p, v, issue, cfg, rng),
                          p.unit_price * creep_factor(creep, v, p, mi), p.vat_rate) for p in v.products]
                out.append(ap_invoice(ids, v, rec, company, issue, build_lines(items), rng))

    variable = [v for v in vendors if v.cadence == "variable"]
    total_w = sum(v.weight for v in variable)
    season = np.array([SEASONALITY[m.month - 1] for m in months])
    season = season / season.sum()
    for v in variable:
        count = max(2, round(n_variable * v.weight / total_w))
        for mi in rng.choice(len(months), size=count, p=season):
            ms = months[mi]
            dim = calendar.monthrange(ms.year, ms.month)[1]
            issue = next_business_day(ms + timedelta(days=int(rng.integers(dim))))
            if issue > cfg.end:
                continue
            n_lines = min(len(v.products), 1 + int(rng.poisson(1.0)))
            chosen = rng.choice(len(v.products), size=n_lines, replace=False)
            target = TARGET_AMOUNT[v.category] * rng.lognormal(0, 0.45)
            items = []
            for pi in chosen:
                p = v.products[pi]
                up = p.unit_price * creep_factor(creep, v, p, mi)
                qty = max(1, round(target / n_lines / float(up)))
                items.append((p.name, Decimal(qty), up, p.vat_rate))
            out.append(ap_invoice(ids, v, records[v.id], company, issue, build_lines(items), rng))
    return out


# ---------------------------------------------------------------- AR


def generate_ar(cfg: SimConfig, rng: np.random.Generator, ids: IdSource, company: Company,
                customers: list[Customer], count: int) -> list[Invoice]:
    months = cfg.month_starts
    season = np.array([SEASONALITY[m.month - 1] for m in months])
    season = season / season.sum()
    weights = np.array([c.weight for c in customers])
    weights = weights / weights.sum()
    dates = []
    for mi in rng.choice(len(months), size=count, p=season):
        ms = months[mi]
        dim = calendar.monthrange(ms.year, ms.month)[1]
        d = next_business_day(ms + timedelta(days=int(rng.integers(dim))))
        if d <= cfg.end:
            dates.append(d)
    out = []
    for seq, issue in enumerate(sorted(dates), start=1):
        c = customers[int(rng.choice(len(customers), p=weights))]
        n_lines = 1 + int(rng.integers(4))
        items = []
        for pi in rng.choice(len(catalog.AR_PRODUCTS), size=n_lines, replace=False):
            name, price, rate = catalog.AR_PRODUCTS[pi]
            qty = 1 if "forfait" in name else max(1, round(rng.lognormal(1.17, 0.7)))
            items.append((name, Decimal(qty), Decimal(str(price)), Decimal(rate)))
        out.append(make_invoice(
            ids, direction="AR", seller=company, buyer=c, number=f"FC-{issue.year}-{seq:05d}",
            issue=issue, terms_days=c.payment_terms_days, lines=build_lines(items), fmt="facturx",
            template=0, customer_id=c.id,
        ))
    return out
