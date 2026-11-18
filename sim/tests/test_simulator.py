from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from decimal import Decimal

import facturx
import pytest

from ledgerline_sim.answer_key import doc_status
from ledgerline_sim.config import SimConfig
from ledgerline_sim.export import doc_path, render_documents, write_world
from ledgerline_sim.ids import iban_valid, luhn_valid
from ledgerline_sim.plant import DUPLICATE_PLAN
from ledgerline_sim.render.documents import render_document
from ledgerline_sim.render.xml import build_cii, build_ubl
from ledgerline_sim.world import generate

logging.getLogger("factur-x").setLevel(logging.ERROR)


@pytest.fixture(scope="module")
def world():
    return generate(SimConfig(seed=42))


def test_same_seed_same_company(world):
    again = generate(SimConfig(seed=42))
    assert again.answer_key == world.answer_key
    assert [t.model_dump() for t in again.txns] == [t.model_dump() for t in world.txns]


def test_different_seed_different_company(world):
    other = generate(SimConfig(seed=7))
    assert other.answer_key["summary"] != world.answer_key["summary"]


def test_volumes(world):
    s = world.answer_key["summary"]
    assert s["invoices"] == 2000
    assert len({v.id for v in world.vendors}) >= 40
    assert 1000 < s["bank_transactions"] < 3000
    assert {i.issue_date.month for i in world.invoices} == set(range(1, 13))


def test_planted_issue_counts(world):
    ak = world.answer_key
    assert len(ak["duplicates"]) == 25
    assert [(d["type"], d["paid"]) for d in ak["duplicates"]] == DUPLICATE_PLAN
    assert Decimal(ak["summary"]["duplicate_recoverable_eur"]) > 0
    assert len(ak["fraud"]) == 3
    assert {f["type"] for f in ak["fraud"]} == {"bank_change_email", "new_vendor_urgent",
                                               "tampered_invoice_iban"}
    assert len(ak["vendor_clusters"]) == 5
    assert len(ak["price_creep"]) == 2
    assert len(ak["unused_subscriptions"]) == 3
    assert {p["name"] for p in ak["late_payers"]} == {
        "Cowork Nation SAS", "Studio Architecture Lemaire", "Restaurants Bouchon & Co"}
    assert len(ak["negatives"]["near_duplicate_legit"]) == 6
    assert len(ak["legit_bank_changes"]) == 1


def test_duplicates_are_really_duplicates(world):
    docs = {i.doc_id: i for i in world.invoices}
    for d in world.answer_key["duplicates"]:
        orig, dup = docs[d["original_doc_id"]], docs[d["duplicate_doc_id"]]
        assert orig.vendor_id == dup.vendor_id
        if d["type"] == "unapplied_credit_note":
            assert dup.doc_type == "credit_note" and dup.references_invoice == orig.doc_id
            continue
        assert orig.total == dup.total
        if d["type"] == "exact":
            assert orig.number == dup.number and orig.vendor_record_id == dup.vendor_record_id
        if d["type"] == "changed_reference":
            assert orig.number != dup.number
        if d["type"] == "two_channel":
            assert orig.format in ("facturx", "ubl", "cii") and dup.format in ("scan", "pdf")
        if d["type"] == "vendor_twice":
            assert orig.vendor_record_id != dup.vendor_record_id


def test_fraud_invoices_are_unpaid_and_in_payment_run(world, tmp_path):
    status = doc_status(world)
    for f in world.answer_key["fraud"]:
        assert status[f["doc_id"]][0] == "open"
        assert f["fraud_iban"] != f["iban_on_file"] or not f["siren_valid"]
    write_world(world, tmp_path, render=False)
    with (tmp_path / "payment_run_proposed.csv").open(encoding="utf-8") as fh:
        run = {r["doc_id"] for r in csv.DictReader(fh)}
    assert {f["doc_id"] for f in world.answer_key["fraud"]} <= run


def test_identifiers_are_checksum_valid(world):
    fraud_vendor = next(f["vendor_id"] for f in world.answer_key["fraud"] if not f["siren_valid"])
    for v in world.vendors:
        assert iban_valid(v.iban), v.name
        if v.siren:
            assert luhn_valid(v.siren) == (v.id != fraud_vendor), v.name
    for c in world.customers:
        assert luhn_valid(c.siren) and iban_valid(c.iban)
    assert all(iban_valid(i.iban) for i in world.invoices)


def test_invoice_arithmetic(world):
    for inv in world.invoices:
        assert inv.net == sum(ln.amount for ln in inv.lines)
        assert inv.total == inv.net + inv.vat
        assert inv.due_date >= inv.issue_date


def test_every_match_points_at_real_things(world):
    txns = {t.txn_id for t in world.txns}
    docs = {i.doc_id for i in world.invoices}
    assert all(m.txn_id in txns and m.doc_id in docs for m in world.matches)
    kinds = Counter(len([m for m in world.matches if m.txn_id == t]) for t in {m.txn_id for m in world.matches})
    assert kinds[1] > 0 and any(k > 1 for k in kinds)  # one-to-one and one-to-many both exist
    assert any(s == "partially_paid" for s, _ in doc_status(world).values())


def test_einvoicing_share_rises_after_reform(world):
    from ledgerline_sim.config import EINVOICING_REFORM

    def share(invs):
        return sum(i.format in ("facturx", "ubl", "cii") for i in invs) / len(invs)

    before = [i for i in world.ap if i.issue_date < EINVOICING_REFORM]
    after = [i for i in world.ap if i.issue_date >= EINVOICING_REFORM]
    assert share(after) > share(before)


def test_structured_xml_validates_against_xsd(world):
    sample = ([i for i in world.invoices if i.doc_type == "credit_note"][:3]
              + [i for i in world.ap if i.currency == "USD"][:2] + world.ar[:3] + world.ap[:10])
    for inv in sample:
        assert facturx.xml_check_xsd(build_cii(inv), flavor="factur-x")
        flavor = "ubl-2.1-creditnote" if inv.doc_type == "credit_note" else "ubl-2.1-invoice"
        assert facturx.xml_check_xsd(build_ubl(inv), flavor=flavor)


def test_facturx_pdf_round_trip(world):
    inv = next(i for i in world.ap if i.format == "facturx")
    pdf = render_document(inv, world.cfg.seed)
    _, xml = facturx.get_xml_from_pdf(pdf, check_xsd=True)
    assert inv.number.encode() in xml and f"{inv.total:.2f}".encode() in xml


def test_scan_is_an_image_only_pdf(world):
    inv = next(i for i in world.ap if i.format == "scan")
    pdf = render_document(inv, world.cfg.seed)
    assert pdf.startswith(b"%PDF") and inv.number.encode() not in pdf


def test_export_layout_and_no_answer_leakage(world, tmp_path):
    write_world(world, tmp_path, render=False)
    for name in ("company.json", "vendor_master.csv", "customers.csv", "saas_usage.csv",
                 "bank/statement.csv", "answer_key.json", "ground_truth/invoices.jsonl",
                 "ground_truth/matches.csv"):
        assert (tmp_path / name).exists(), name
    assert len(list((tmp_path / "bank/camt053").glob("*.xml"))) == 12
    assert len(list((tmp_path / "emails").glob("*.eml"))) == len(world.emails)
    statement = (tmp_path / "bank/statement.csv").read_text(encoding="utf-8-sig")
    assert "supplier" not in statement and "payroll" not in statement
    master = (tmp_path / "vendor_master.csv").read_text(encoding="utf-8")
    assert "vendor_id" not in master.splitlines()[0]
    key = json.loads((tmp_path / "answer_key.json").read_text(encoding="utf-8"))
    assert key["summary"]["fraud_attempts"] == 3


def test_render_writes_every_format(world, tmp_path):
    picks = {}
    for inv in world.invoices:
        picks.setdefault(inv.format, inv)
    sub = type(world)(**{**world.__dict__, "invoices": list(picks.values())})
    (tmp_path / "invoices/ap").mkdir(parents=True)
    (tmp_path / "invoices/ar").mkdir(parents=True)
    render_documents(sub, tmp_path, workers=1)
    for inv in picks.values():
        assert (tmp_path / doc_path(inv)).stat().st_size > 500
    assert set(picks) == {"facturx", "ubl", "cii", "pdf", "scan"}
