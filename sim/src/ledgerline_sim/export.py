"""Writes a generated World to disk."""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from lxml import etree

from .answer_key import doc_status
from .emails import to_eml
from .models import BankTxn, Invoice
from .render.documents import EXTENSION, render_document
from .world import World

CAMT = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"


def doc_path(inv: Invoice) -> str:
    return f"invoices/{inv.direction.lower()}/{inv.doc_id}.{EXTENSION[inv.format]}"


def write_world(w: World, out: Path, render: bool = True, workers: int | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for sub in ("invoices/ap", "invoices/ar", "bank/camt053", "emails", "ground_truth"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    _json(out / "company.json", w.company.model_dump(mode="json"))
    _csv(out / "vendor_master.csv", [
        {"record_id": r.record_id, "name": r.name, "siren": r.siren or "", "vat_number": r.vat_number or "",
         "iban": r.iban, "phone": r.phone, "email": r.email, "street": r.address.street,
         "postcode": r.address.postcode, "city": r.address.city, "country": r.address.country,
         "created_on": r.created_on.isoformat()}
        for r in sorted(w.records, key=lambda r: r.record_id)
    ])
    _csv(out / "customers.csv", [
        {"customer_id": c.id, "name": c.name, "siren": c.siren, "vat_number": c.vat_number, "iban": c.iban,
         "street": c.address.street, "postcode": c.address.postcode, "city": c.address.city,
         "payment_terms_days": c.payment_terms_days}
        for c in w.customers
    ])
    _csv(out / "saas_usage.csv", [u.model_dump() for u in w.usage])

    write_bank_csv(w, out / "bank" / "statement.csv")
    write_camt053(w, out / "bank" / "camt053")
    for e in w.emails:
        (out / "emails" / f"{e.email_id}.eml").write_bytes(to_eml(e))

    status = doc_status(w)
    horizon = w.cfg.end + timedelta(days=w.cfg.payment_run_horizon_days)
    _csv(out / "payment_run_proposed.csv", [
        {"doc_id": i.doc_id, "vendor_record_id": i.vendor_record_id, "supplier": i.seller_name,
         "invoice_number": i.number, "due_date": i.due_date.isoformat(), "amount": str(i.total),
         "currency": i.currency, "iban": i.iban}
        for i in sorted(w.ap, key=lambda i: i.due_date)
        if i.doc_type == "invoice" and status[i.doc_id][0] == "open" and i.due_date <= horizon
    ])

    gt = out / "ground_truth"
    with (gt / "invoices.jsonl").open("w", encoding="utf-8") as f:
        for inv in w.invoices:
            st, settled = status[inv.doc_id]
            row = inv.model_dump(mode="json")
            row.update(file=doc_path(inv), status=st, settled_eur=str(settled))
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _csv(gt / "matches.csv", [m.model_dump(mode="json") for m in w.matches])
    _csv(gt / "bank_categories.csv", [{"txn_id": t.txn_id, "category": t.category} for t in w.txns])
    _csv(gt / "vendor_entities.csv", [{"record_id": r.record_id, "vendor_id": r.vendor_id}
                                      for r in sorted(w.records, key=lambda r: r.record_id)])
    _json(out / "answer_key.json", w.answer_key)
    (out / "README.md").write_text(_readme(w), encoding="utf-8")

    if render:
        render_documents(w, out, workers)


def render_documents(w: World, out: Path, workers: int | None = None) -> None:
    jobs = [(inv, w.cfg.seed, str(out / doc_path(inv))) for inv in w.invoices]
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    if workers == 1:
        for job in jobs:
            _render_one(job)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_render_one, jobs, chunksize=25))


def _render_one(job: tuple[Invoice, int, str]) -> None:
    inv, seed, path = job
    Path(path).write_bytes(render_document(inv, seed))


# ------------------------------------------------------------------ bank


def _fr_amount(x: Decimal) -> str:
    return f"{x:.2f}".replace(".", ",")


def write_bank_csv(w: World, path: Path) -> None:
    """French bank export: ';' separated, decimal comma, debit/credit columns."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(["Date opération", "Date valeur", "Libellé", "Contrepartie", "IBAN contrepartie",
                     "Débit", "Crédit", "Devise", "Montant devise", "Devise d'origine", "Référence banque"])
        for t in w.txns:
            wr.writerow([
                t.booking_date.strftime("%d/%m/%Y"), t.value_date.strftime("%d/%m/%Y"), t.reference,
                t.counterparty_name, t.counterparty_iban or "",
                _fr_amount(-t.amount) if t.amount < 0 else "", _fr_amount(t.amount) if t.amount > 0 else "",
                t.currency, _fr_amount(t.original_amount) if t.original_amount else "",
                t.original_currency or "", t.txn_id,
            ])


def write_camt053(w: World, folder: Path) -> None:
    by_month: dict[str, list[BankTxn]] = defaultdict(list)
    for t in w.txns:
        by_month[t.booking_date.strftime("%Y-%m")].append(t)
    balance = w.company.opening_balance
    for ms in w.cfg.month_starts:
        key = ms.strftime("%Y-%m")
        txns = by_month.get(key, [])
        closing = balance + sum((t.amount for t in txns), Decimal("0"))
        (folder / f"{key}.xml").write_bytes(_camt_statement(w, ms, txns, balance, closing))
        balance = closing


def _camt_statement(w: World, ms, txns: list[BankTxn], opening: Decimal, closing: Decimal) -> bytes:
    def E(parent, tag, text=None, **attrs):
        el = etree.SubElement(parent, f"{{{CAMT}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    month_end = (ms.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    created = datetime.combine(month_end + timedelta(days=1), datetime.min.time()).isoformat()
    root = etree.Element(f"{{{CAMT}}}Document", nsmap={None: CAMT})
    stmt_root = E(root, "BkToCstmrStmt")
    hdr = E(stmt_root, "GrpHdr")
    E(hdr, "MsgId", f"CAMT053-{ms:%Y%m}")
    E(hdr, "CreDtTm", created)
    stmt = E(stmt_root, "Stmt")
    E(stmt, "Id", f"STMT-{ms:%Y%m}")
    E(stmt, "CreDtTm", created)
    period = E(stmt, "FrToDt")
    E(period, "FrDtTm", f"{ms.isoformat()}T00:00:00")
    E(period, "ToDtTm", f"{month_end.isoformat()}T23:59:59")
    acct = E(stmt, "Acct")
    E(E(acct, "Id"), "IBAN", w.company.bank_account_iban)
    E(acct, "Ccy", "EUR")
    for code, amount, d in (("OPBD", opening, ms), ("CLBD", closing, month_end)):
        bal = E(stmt, "Bal")
        E(E(E(bal, "Tp"), "CdOrPrtry"), "Cd", code)
        E(bal, "Amt", f"{abs(amount):.2f}", Ccy="EUR")
        E(bal, "CdtDbtInd", "CRDT" if amount >= 0 else "DBIT")
        E(E(bal, "Dt"), "Dt", d.isoformat())
    for t in txns:
        credit = t.amount > 0
        n = E(stmt, "Ntry")
        E(n, "Amt", f"{abs(t.amount):.2f}", Ccy="EUR")
        E(n, "CdtDbtInd", "CRDT" if credit else "DBIT")
        E(E(n, "Sts"), "Cd", "BOOK")
        E(E(n, "BookgDt"), "Dt", t.booking_date.isoformat())
        E(E(n, "ValDt"), "Dt", t.value_date.isoformat())
        E(n, "AcctSvcrRef", t.txn_id)
        E(E(E(n, "BkTxCd"), "Prtry"), "Cd", t.bank_code)
        tx = E(E(n, "NtryDtls"), "TxDtls")
        E(E(tx, "Refs"), "EndToEndId", "NOTPROVIDED")
        if t.original_amount is not None:
            E(E(E(tx, "AmtDtls"), "InstdAmt"), "Amt", f"{t.original_amount:.2f}", Ccy=t.original_currency)
        parties = E(tx, "RltdPties")
        role = "Dbtr" if credit else "Cdtr"
        E(E(E(parties, role), "Pty"), "Nm", t.counterparty_name)
        if t.counterparty_iban:
            E(E(E(parties, f"{role}Acct"), "Id"), "IBAN", t.counterparty_iban)
        if t.reference:
            E(E(tx, "RmtInf"), "Ustrd", t.reference)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ------------------------------------------------------------------ helpers


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)


def _json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _readme(w: World) -> str:
    s = w.answer_key["summary"]
    return f"""# {w.company.name} (synthetic, seed {w.cfg.seed})

Period {w.cfg.start} to {w.cfg.end}. Fictional company and data, generated by ledgerline-sim.

## Inputs (what Ledgerline is allowed to read)
- `invoices/ap/`, `invoices/ar/`: {s['ap_invoices']} supplier and {s['ar_invoices']} customer documents
  (Factur-X PDF, UBL / CII XML, plain PDF, scanned PDF)
- `bank/statement.csv` (French export, `;` + decimal comma) and `bank/camt053/*.xml` (ISO 20022)
- `vendor_master.csv`, `customers.csv`, `saas_usage.csv`, `emails/*.eml`
- `payment_run_proposed.csv`: open supplier invoices due within {w.cfg.payment_run_horizon_days} days of period end

## Answers (never read by the product; used only by evals)
- `answer_key.json`: every planted problem and the money at stake
- `ground_truth/`: true invoice fields, txn↔invoice matches, bank categories, vendor entities

## Planted
{s['duplicates']} duplicate payments ({s['duplicates_paid']} paid, EUR {s['duplicate_recoverable_eur']} recoverable),
{s['fraud_attempts']} fraud attempts (EUR {s['fraud_amount_eur']}), {s['vendor_clusters']} vendors saved more than once,
{s['price_creep_vendors']} vendors with price creep, {s['unused_subscriptions']} unused subscriptions,
{s['late_payers']} late-paying customers.
"""
