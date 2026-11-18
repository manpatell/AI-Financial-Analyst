from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

from ledgerline.ledger.models import (
    BankTransaction,
    CalcOutput,
    Customer,
    Decision,
    Invoice,
    InvoiceLine,
    Match,
    Vendor,
)

EXPECTED_TABLES = {"vendors", "customers", "invoices", "invoice_lines", "bank_transactions", "emails",
                   "matches", "findings", "decisions", "calc_outputs"}


def _ap_invoice(vendor: Vendor, sha: str = "a" * 64, **kw) -> Invoice:
    data = dict(direction="AP", vendor=vendor, number="F2026-0042", issue_date=date(2026, 3, 2),
                due_date=date(2026, 4, 1), net=Decimal("1000.00"), vat=Decimal("200.00"),
                total=Decimal("1200.00"), source_format="facturx", source_file="invoices/ap/AP-00001.pdf",
                sha256=sha)
    data.update(kw)
    return Invoice(**data)


def test_schema_has_all_tables(engine):
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_money_round_trips_exactly(session):
    v = Vendor(name="Métaux Rhône-Alpes SAS", siren="948677133")
    inv = _ap_invoice(v, net=Decimal("0.10"), vat=Decimal("0.02"), total=Decimal("0.12"))
    inv.lines = [InvoiceLine(line_no=1, description="Visserie", quantity=Decimal("3"),
                             unit_price=Decimal("0.0333"), vat_rate=Decimal("20"), amount=Decimal("0.10"))]
    session.add(inv)
    session.flush()
    session.expire_all()
    got = session.scalars(select(Invoice)).one()
    assert got.total == Decimal("0.12") and isinstance(got.total, Decimal)
    assert got.net + got.vat == got.total
    assert got.lines[0].unit_price == Decimal("0.0333")


def test_invoice_lines_cascade_on_delete(session):
    inv = _ap_invoice(Vendor(name="V"))
    inv.lines = [InvoiceLine(line_no=1, description="x", amount=Decimal("1000.00"))]
    session.add(inv)
    session.flush()
    session.delete(inv)
    session.flush()
    assert session.scalars(select(InvoiceLine)).all() == []


def test_same_document_cannot_be_ingested_twice(session):
    v = Vendor(name="V")
    session.add_all([_ap_invoice(v, sha="b" * 64), _ap_invoice(v, sha="b" * 64)])
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize("bad", [
    dict(direction="XX"),
    dict(source_format="docx"),
    dict(doc_type="receipt"),
])
def test_check_constraints(session, bad):
    session.add(_ap_invoice(Vendor(name="V"), **bad))
    with pytest.raises(IntegrityError):
        session.flush()


def test_ap_invoice_cannot_have_a_customer(session):
    session.add(_ap_invoice(Vendor(name="V"), customer=Customer(name="C")))
    with pytest.raises(IntegrityError):
        session.flush()


def test_match_links_txn_and_invoice(session):
    v = Vendor(name="V")
    inv = _ap_invoice(v)
    txn = BankTransaction(bank_ref="TXN-000001", account_iban="FR76300040000100000000000", amount=Decimal("-1200.00"),
                          booking_date=date(2026, 4, 2), source_file="bank/statement.csv")
    d = Decision(actor="rule-engine", action="match.auto_accept", target={"txn": 1},
                 rule_applied="exact_amount_and_reference", confidence=Decimal("0.99"))
    session.add_all([inv, txn, d])
    session.flush()
    session.add(Match(txn_id=txn.id, invoice_id=inv.id, allocated_amount=Decimal("1200.00"), method="rule",
                      status="accepted", decision_id=d.id))
    session.flush()
    assert session.scalars(select(Match)).one().allocated_amount == Decimal("1200.00")


# ------------------------------------------------------------- append-only audit


def _decision(session) -> Decision:
    d = Decision(actor="agent", action="payment.hold", target={"invoice_ids": [1]},
                 model_version="claude-sonnet-5-5", input_lineage={"inv": [1]}, confidence=Decimal("0.93"))
    session.add(d)
    session.flush()
    return d


def test_decisions_accept_inserts_and_approvals_chain(session):
    d = _decision(session)
    approval = Decision(actor="user:camille", action="approve", target={"decision_id": d.id}, parent_id=d.id)
    session.add(approval)
    session.flush()
    assert session.get(Decision, approval.id).parent_id == d.id


@pytest.mark.parametrize("sql", [
    "UPDATE decisions SET actor = 'someone-else'",
    "DELETE FROM decisions",
    "TRUNCATE decisions CASCADE",
    "UPDATE calc_outputs SET tool = 'x'",
    "DELETE FROM calc_outputs",
])
def test_audit_tables_are_append_only(session, sql):
    _decision(session)
    session.add(CalcOutput(id="7f3a", tool="get_cash_position", inputs={}, result={"cash": "1.00"}))
    session.flush()
    with pytest.raises((InternalError, ProgrammingError), match="append-only"):
        session.execute(text(sql))


def test_orm_update_of_decision_is_rejected(session):
    d = _decision(session)
    d.confidence = Decimal("0.10")
    with pytest.raises((InternalError, ProgrammingError), match="append-only"):
        session.flush()


def test_migration_downgrade_and_upgrade(alembic_cfg, engine):
    command.downgrade(alembic_cfg, "base")
    assert not (EXPECTED_TABLES & set(inspect(engine).get_table_names()))
    command.upgrade(alembic_cfg, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
