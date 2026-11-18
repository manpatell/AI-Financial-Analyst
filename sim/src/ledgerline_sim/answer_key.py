"""Builds the answer key: every planted issue, with final doc / txn / email ids and money at stake."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from .catalog import LATE_PAYER_THRESHOLD_DAYS
from .plant import UNUSED_SUBSCRIPTIONS

if TYPE_CHECKING:
    from .world import World

ZERO = Decimal("0.00")


def doc_status(w: World) -> dict[str, tuple[str, Decimal]]:
    """doc_id -> (paid | partially_paid | open, EUR settled). Card FX payments count as paid."""
    settled: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for m in w.matches:
        settled[m.doc_id] += m.allocated_amount
    out = {}
    for inv in w.invoices:
        s = settled[inv.doc_id]
        target = inv.signed_total
        if s == ZERO:
            out[inv.doc_id] = ("open", ZERO)
        elif s == target or inv.currency != "EUR":
            out[inv.doc_id] = ("paid", s)
        else:
            out[inv.doc_id] = ("partially_paid", s)
    return out


def _docs_by_tag(w: World) -> dict[str, dict[str, str]]:
    """plant_id -> {role: doc_id}"""
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for inv in w.invoices:
        for t in inv.tags:
            parts = t.split(":")
            if len(parts) == 3:
                out[parts[1]][parts[2]] = inv.doc_id
    for e in w.emails:
        for t in e.tags:
            _, pid, role = t.split(":")
            out[pid][role] = e.email_id
    return out


def build_answer_key(w: World, meta: dict[str, dict]) -> dict:
    status = doc_status(w)
    docs = {i.doc_id: i for i in w.invoices}
    tagged = _docs_by_tag(w)
    vendors = {v.id: v for v in w.vendors}

    duplicates, frauds, clusters, creep, bank_changes = [], [], [], [], []
    for pid, m in sorted(meta.items()):
        roles = tagged.get(pid, {})
        if m["kind"] == "duplicate":
            orig = docs[roles["original"]]
            if m["type"] == "unapplied_credit_note":
                cn = docs[roles["credit_note"]]
                paid = status[orig.doc_id][0] == "paid"
                duplicates.append({
                    "id": pid, "type": m["type"], "vendor_id": m["vendor_id"],
                    "original_doc_id": orig.doc_id, "duplicate_doc_id": cn.doc_id,
                    "amount": str(cn.total), "paid": paid, "recoverable": str(cn.total if paid else ZERO),
                    "at_risk": str(ZERO),
                })
                continue
            dup = docs[roles["duplicate"]]
            st, settled = status[dup.doc_id]
            duplicates.append({
                "id": pid, "type": m["type"], "vendor_id": m["vendor_id"],
                "original_doc_id": orig.doc_id, "duplicate_doc_id": dup.doc_id,
                "original_number": orig.number, "duplicate_number": dup.number,
                "amount": str(dup.total), "paid": st == "paid",
                "recoverable": str(settled), "at_risk": str(dup.total - settled),
            })
        elif m["kind"] == "fraud":
            inv = docs[roles["invoice"]]
            frauds.append({
                "id": pid, "type": m["type"], "vendor_id": m["vendor_id"], "doc_id": inv.doc_id,
                "email_id": roles.get("email"), "amount": str(inv.total), "fraud_iban": m["fraud_iban"],
                "iban_on_file": m["iban_on_file"], "due_date": inv.due_date.isoformat(),
                "siren_valid": m.get("siren_valid", True),
            })
        elif m["kind"] == "vendor_cluster":
            clusters.append({"id": pid, "vendor_id": m["vendor_id"], "record_ids": m["record_ids"]})
        elif m["kind"] == "legit_bank_change":
            bank_changes.append({**{k: v for k, v in m.items() if k != "kind"}, "id": pid,
                                 "email_id": roles.get("email")})
        elif m["kind"] == "price_creep":
            v = vendors[m["vendor_id"]]
            base = {p.name: p.unit_price for p in v.products}
            per_product = []
            for pname in m["products"]:
                lines = sorted(((inv.issue_date, ln) for inv in w.invoices if inv.vendor_id == v.id
                                and inv.doc_type == "invoice" for ln in inv.lines if ln.description == pname),
                               key=lambda x: x[0])
                if not lines:
                    continue
                excess = sum(((ln.unit_price - base[pname]) * ln.quantity for _, ln in lines), ZERO)
                per_product.append({
                    "product": pname, "first_price": str(lines[0][1].unit_price),
                    "last_price": str(lines[-1][1].unit_price),
                    "excess_paid_net": str(excess.quantize(Decimal("0.01"))),
                })
            creep.append({"id": pid, "vendor_id": v.id, "monthly_rate": m["monthly_rate"],
                          "products": per_product,
                          "excess_paid_net": str(sum((Decimal(p["excess_paid_net"]) for p in per_product),
                                                     ZERO))})

    unused = []
    for i, (name, from_month) in enumerate(UNUSED_SUBSCRIPTIONS, start=1):
        v = next(x for x in w.vendors if x.name == name)
        cutoff = w.cfg.month_starts[from_month]
        wasted = [status[inv.doc_id][1] for inv in w.invoices
                  if inv.vendor_id == v.id and inv.issue_date >= cutoff]
        monthly = wasted[-1] if wasted else ZERO
        unused.append({"id": f"unused-{i:02d}", "vendor_id": v.id, "tool": name,
                       "unused_since": cutoff.isoformat(), "months_unused": len(wasted),
                       "wasted_to_date_eur": str(sum(wasted, ZERO)),
                       "annualised_eur": str(monthly * 12)})

    late = _late_payers(w, status)

    negatives = {
        "near_duplicate_legit": [
            {"original_doc_id": r["original"], "repeat_doc_id": r["legit_repeat"]}
            for pid, r in sorted(tagged.items()) if pid.startswith("negdup-")
        ],
        "applied_credit_notes": [i.doc_id for i in w.invoices if "credit_note:applied" in i.tags],
        "legit_bank_changes": [b["id"] for b in bank_changes],
    }

    recoverable = sum((Decimal(d["recoverable"]) for d in duplicates), ZERO)
    at_risk = sum((Decimal(d["at_risk"]) for d in duplicates), ZERO)
    return {
        "seed": w.cfg.seed,
        "period": {"start": w.cfg.start.isoformat(), "end": w.cfg.end.isoformat()},
        "summary": {
            "invoices": len(w.invoices), "ap_invoices": len(w.ap), "ar_invoices": len(w.ar),
            "bank_transactions": len(w.txns), "emails": len(w.emails),
            "duplicates": len(duplicates), "duplicates_paid": sum(d["paid"] for d in duplicates),
            "duplicate_recoverable_eur": str(recoverable), "duplicate_at_risk_eur": str(at_risk),
            "fraud_attempts": len(frauds),
            "fraud_amount_eur": str(sum((Decimal(f["amount"]) for f in frauds), ZERO)),
            "vendor_clusters": len(clusters), "price_creep_vendors": len(creep),
            "unused_subscriptions": len(unused), "late_payers": len(late),
            "opening_balance": str(w.company.opening_balance),
            "closing_balance": str(w.closing_balance()),
        },
        "duplicates": duplicates,
        "fraud": frauds,
        "legit_bank_changes": bank_changes,
        "vendor_clusters": clusters,
        "price_creep": creep,
        "unused_subscriptions": unused,
        "late_payers": late,
        "negatives": negatives,
    }


def _late_payers(w: World, status: dict) -> list[dict]:
    txn_date = {t.txn_id: t.booking_date for t in w.txns}
    last_payment: dict[str, object] = {}
    for m in w.matches:
        d = txn_date[m.txn_id]
        if m.doc_id not in last_payment or d > last_payment[m.doc_id]:
            last_payment[m.doc_id] = d
    days: dict[str, list[int]] = defaultdict(list)
    overdue: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for inv in w.ar:
        if status[inv.doc_id][0] == "paid":
            days[inv.customer_id].append((last_payment[inv.doc_id] - inv.due_date).days)
        elif inv.due_date + timedelta(days=0) < w.cfg.end:
            overdue[inv.customer_id] += inv.total - status[inv.doc_id][1]
    out = []
    for c in w.customers:
        d = days.get(c.id, [])
        avg = sum(d) / len(d) if d else 0.0
        if avg > LATE_PAYER_THRESHOLD_DAYS:
            out.append({"customer_id": c.id, "name": c.name, "avg_days_late": round(avg, 1),
                        "paid_invoices": len(d), "overdue_open_eur": str(overdue[c.id])})
    return out
