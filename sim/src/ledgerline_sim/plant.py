"""Plants known problems into a clean company. Every planted thing carries a tag
"<kind>:<plant_id>:<role>" and a metadata entry in `meta`; answer_key.py turns those into the
answer key after final ids are assigned.

Also plants *negatives* (legit look-alikes) so detectors are measured on precision, not just recall.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from .company import master_record, next_business_day
from .config import SimConfig
from .ids import foreign_iban, fr_iban, fr_vat, siren
from .invoices import IdSource, ap_invoice, build_lines, next_number
from .models import Address, Company, Invoice, Product, Vendor, VendorRecord, money

VARIABLE_CATEGORIES = {"materials", "logistics", "packaging", "services", "marketing", "maintenance"}

# (canonical vendor name, [variant records]) — vendor saved more than once in the ERP.
CLUSTERS = [
    ("Métaux Rhône-Alpes SAS", [dict(name="METAUX RHONE ALPES", siren=True, iban=True)]),
    ("Quincaillerie Pro Bonnard", [dict(name="Quincaillerie Bonnard", siren=False, iban=True)]),
    ("Cartonnages Mérieux", [dict(name="Cartonnages Merieux SARL", siren=True, iban=True,
                                  address_variant=True)]),
    ("Express Fret Lyon", [dict(name="Express-Fret Lyon SAS", siren=True, iban=True, phone_variant=True)]),
    ("Peintures Époxy Saône", [dict(name="Peintures Epoxy Saone", siren=True, iban=True),
                               dict(name="PES Peintures", siren=False, iban=True, vat_only=True)]),
]

PRICE_CREEP = [
    ("Bois & Panneaux du Dauphiné", 0.018, []),
    ("Composants Électriques Vallin", 0.012, ["Moteur vérin bureau assis-debout"]),
]

UNUSED_SUBSCRIPTIONS = [("Huddlecast Video Inc.", 4), ("Vectorly Design Ltd", 6), ("Planifo Cloud", 5)]

LEGIT_BANK_CHANGE_VENDOR = "Transports Garnier Frères"

# (type, paid?) — 25 duplicate-payment cases.
DUPLICATE_PLAN = (
    [("exact", True)] * 4 + [("exact", False)]
    + [("changed_reference", True)] * 6 + [("changed_reference", False)]
    + [("two_channel", True)] * 4 + [("two_channel", False)]
    + [("vendor_twice", True)] * 4 + [("vendor_twice", False)]
    + [("unapplied_credit_note", True)] * 3
)

N_APPLIED_CREDIT_NOTES = 10
N_NEAR_DUPLICATE_NEGATIVES = 6


def _tag(kind: str, pid: str, role: str) -> str:
    return f"{kind}:{pid}:{role}"


def mutate_reference(number: str, rng: np.random.Generator) -> str:
    m = re.search(r"(\d+)\D*$", number)
    digits = m.group(1) if m else number
    candidates = [
        f"{digits.lstrip('0') or '0'}-{['OPS', 'A', 'BIS', '1'][int(rng.integers(4))]}",
        re.sub(r"[-/ ]", "", number),
        number.replace(digits, digits.lstrip("0") or "0"),
        f"N°{number}",
        f"{number}/1",
        number.replace("-", " ").replace("/", " "),
    ]
    candidates = [c for c in candidates if c != number]
    return candidates[int(rng.integers(len(candidates)))]


def lookalike_domain(domain: str, rng: np.random.Generator) -> str:
    name, tld = domain.rsplit(".", 1)
    options = []
    if "i" in name:
        options.append(name.replace("i", "l", 1) + "." + tld)
    if "-" in name:
        options.append(name.replace("-", "", 1) + "." + tld)
    options += [name + "s." + tld, name + ".com" if tld == "fr" else name + ".fr", name + "-fr." + tld]
    return options[int(rng.integers(len(options)))]


class Planter:
    def __init__(self, cfg: SimConfig, rng: np.random.Generator, ids: IdSource, company: Company,
                 vendors: list[Vendor], records: list[VendorRecord]):
        self.cfg, self.rng, self.ids, self.company = cfg, rng, ids, company
        self.vendors = vendors
        self.by_name = {v.name: v for v in vendors}
        self.records = records
        self.main_record = {r.vendor_id: r for r in records}
        self.meta: dict[str, dict] = {}
        self.extra_records: dict[str, list[VendorRecord]] = {}
        self.planned_emails: list[dict] = []  # turned into Email objects by emails.py
        self._used: set[str] = set()

    # ------------------------------------------------------------ before invoices

    def price_creep_rules(self) -> dict:
        rules = {}
        for i, (name, rate, products) in enumerate(PRICE_CREEP, start=1):
            v = self.by_name[name]
            pid = f"creep-{i:02d}"
            rules[v.id] = {"monthly_rate": rate, "products": products, "plant_id": pid}
            self.meta[pid] = {"kind": "price_creep", "vendor_id": v.id, "monthly_rate": rate,
                              "products": products or [p.name for p in v.products]}
        return rules

    def vendor_clusters(self) -> None:
        next_rec = len(self.records) + 1
        for i, (name, variants) in enumerate(CLUSTERS, start=1):
            v = self.by_name[name]
            pid = f"vendor-{i:02d}"
            ids = [self.main_record[v.id].record_id]
            for var in variants:
                created = self.cfg.start + timedelta(days=int(self.rng.integers(-60, 110)))
                addr = v.address
                if var.get("address_variant"):
                    addr = Address(street=addr.street.replace("rue", "Rue").replace(" ", ", ", 1),
                                   postcode=addr.postcode, city=addr.city.upper())
                rec = master_record(
                    v, f"VR{next_rec:03d}", created, name=var["name"],
                    siren=v.siren if var["siren"] else None,
                    vat_number=v.vat_number if (var["siren"] or var.get("vat_only")) else None,
                    iban=v.iban, address=addr,
                    phone=v.phone.replace(" ", ".") if var.get("phone_variant") else v.phone,
                    email=f"factures@{v.email_domain}",
                )
                next_rec += 1
                self.records.append(rec)
                self.extra_records.setdefault(v.id, []).append(rec)
                ids.append(rec.record_id)
            self.meta[pid] = {"kind": "vendor_cluster", "vendor_id": v.id, "record_ids": ids}

    # ------------------------------------------------------------ after clean invoices

    def plant_all(self, ap: list[Invoice]) -> list[Invoice]:
        self._book_under_duplicate_records(ap)
        self._legit_bank_change(ap)
        ap += self._duplicates(ap)
        ap += self._applied_credit_notes(ap)
        ap += self._near_duplicate_negatives(ap)
        ap += self._fraud()
        return ap

    def _book_under_duplicate_records(self, ap: list[Invoice]) -> None:
        for vid, recs in self.extra_records.items():
            v = next(x for x in self.vendors if x.id == vid)
            for inv in ap:
                if inv.vendor_id != vid or self.rng.random() > 0.25:
                    continue
                rec = recs[int(self.rng.integers(len(recs)))]
                if inv.issue_date < rec.created_on:
                    continue
                self._apply_record(inv, v, rec)

    @staticmethod
    def _apply_record(inv: Invoice, v: Vendor, rec: VendorRecord) -> None:
        inv.vendor_record_id = rec.record_id
        inv.seller_name = rec.name
        inv.seller_siren = rec.siren
        inv.seller_vat = rec.vat_number
        inv.seller_address = rec.address

    def _legit_bank_change(self, ap: list[Invoice]) -> None:
        v = self.by_name[LEGIT_BANK_CHANGE_VENDOR]
        change = next_business_day(self.cfg.month_starts[5] + timedelta(days=9))
        old, new = v.iban, fr_iban(self.rng, bank_code="18706")
        for inv in ap:
            if inv.vendor_id == v.id and inv.issue_date >= change:
                inv.iban = new
        self.main_record[v.id].iban = new
        pid = "bankchange-01"
        self.meta[pid] = {"kind": "legit_bank_change", "vendor_id": v.id, "old_iban": old,
                          "new_iban": new, "effective_date": change.isoformat()}
        self.planned_emails.append({
            "kind": "legit_bank_change", "plant_id": pid, "vendor": v, "date": change - timedelta(days=7),
            "new_iban": new, "from_domain": v.email_domain,
        })

    # -- duplicates

    def _candidates(self, ap: list[Invoice], paid: bool, structured: bool = False,
                    cluster: bool = False) -> list[Invoice]:
        cluster_vendors = set(self.extra_records)
        lo, hi = (
            (self.cfg.start + timedelta(days=30), self.cfg.end - timedelta(days=100)) if paid
            else (self.cfg.end - timedelta(days=40), self.cfg.end - timedelta(days=12))
        )
        out = []
        for inv in ap:
            v = next(x for x in self.vendors if x.id == inv.vendor_id)
            if (inv.doc_id in self._used or inv.doc_type != "invoice" or inv.tags
                    or v.category not in VARIABLE_CATEGORIES or v.pay_method != "transfer"
                    or not (lo <= inv.issue_date <= hi)):
                continue
            if cluster != (v.id in cluster_vendors):
                continue
            if cluster and inv.vendor_record_id != self.main_record[v.id].record_id:
                continue
            if structured and v.size not in ("eti", "large", "sme"):
                continue
            out.append(inv)
        return out

    def _pick(self, pool: list[Invoice]) -> Invoice:
        if not pool:
            raise RuntimeError("not enough candidate invoices to plant; increase n_invoices")
        inv = pool[int(self.rng.integers(len(pool)))]
        self._used.add(inv.doc_id)
        return inv

    def _copy(self, inv: Invoice, **changes) -> Invoice:
        dup = inv.model_copy(deep=True)
        dup.doc_id = self.ids.next("AP")
        for k, val in changes.items():
            setattr(dup, k, val)
        dup.due_date = dup.issue_date + (inv.due_date - inv.issue_date)
        dup.channel = {"scan": "post", "pdf": "email"}.get(dup.format, inv.channel)
        return dup

    def _duplicates(self, ap: list[Invoice]) -> list[Invoice]:
        new: list[Invoice] = []
        for i, (dtype, paid) in enumerate(DUPLICATE_PLAN, start=1):
            pid = f"dup-{i:02d}"
            orig = self._pick(self._candidates(ap, paid, structured=dtype == "two_channel",
                                               cluster=dtype == "vendor_twice"))
            v = next(x for x in self.vendors if x.id == orig.vendor_id)
            shift = timedelta(days=int(self.rng.integers(3, 30) if paid else self.rng.integers(2, 9)))
            meta = {"kind": "duplicate", "type": dtype, "paid": paid, "vendor_id": v.id}

            if dtype == "unapplied_credit_note":
                cn = self._credit_note(orig, v, share=self.rng.uniform(0.1, 0.3))
                cn.tags.append(_tag("dup", pid, "credit_note"))
                cn.tags.append("credit_note:unapplied")
                orig.tags.append(_tag("dup", pid, "original"))
                new.append(cn)
                self.meta[pid] = meta
                continue

            if dtype == "exact":
                dup = self._copy(orig)
            elif dtype == "changed_reference":
                dup = self._copy(orig, number=mutate_reference(orig.number, self.rng),
                                 issue_date=orig.issue_date + timedelta(days=int(self.rng.integers(0, 4))))
            elif dtype == "two_channel":
                orig.format = ["facturx", "ubl", "cii"][int(self.rng.integers(3))]
                dup = self._copy(orig, format="scan" if self.rng.random() < 0.6 else "pdf")
            else:  # vendor_twice
                rec = self.extra_records[v.id][0]
                dup = self._copy(orig)
                self._apply_record(dup, v, rec)
                if self.rng.random() < 0.5:
                    dup.number = mutate_reference(orig.number, self.rng)
            if dtype != "two_channel":
                dup.issue_date = max(dup.issue_date, orig.issue_date)
            # The duplicate arrives later than the original; its due date moves with its issue date
            # only when it was re-issued (re-sent copies keep the original dates).
            if dtype in ("changed_reference", "vendor_twice"):
                dup.issue_date = orig.issue_date + shift
                dup.due_date = dup.issue_date + (orig.due_date - orig.issue_date)
            if not paid:
                dup.due_date = max(dup.due_date, self.cfg.end + timedelta(days=3))
                dup.payable = False
            orig.tags.append(_tag("dup", pid, "original"))
            dup.tags.append(_tag("dup", pid, "duplicate"))
            new.append(dup)
            self.meta[pid] = meta
        return new

    def _credit_note(self, orig: Invoice, v: Vendor, share: float) -> Invoice:
        issue = orig.issue_date + timedelta(days=int(self.rng.integers(4, 16)))
        reason = ["Avoir - remise qualité", "Avoir - retour marchandise", "Avoir - erreur de prix",
                  "Avoir - geste commercial"][int(self.rng.integers(4))]
        rate = orig.lines[0].vat_rate
        lines = build_lines([(f"{reason} (facture {orig.number})", Decimal(1),
                              money(orig.net * Decimal(str(share))), rate)])
        number = "AV-" + next_number(v, issue)
        cn = ap_invoice(self.ids, v, self.main_record[v.id], self.company, issue, lines, self.rng,
                        doc_type="credit_note", number=number, references_invoice=orig.doc_id,
                        references_number=orig.number)
        cn.due_date = issue
        cn.vendor_record_id, cn.seller_name = orig.vendor_record_id, orig.seller_name
        return cn

    def _applied_credit_notes(self, ap: list[Invoice]) -> list[Invoice]:
        out = []
        pool = self._candidates(ap, paid=True)
        for _ in range(N_APPLIED_CREDIT_NOTES):
            orig = self._pick(pool)
            pool.remove(orig)
            v = next(x for x in self.vendors if x.id == orig.vendor_id)
            cn = self._credit_note(orig, v, share=self.rng.uniform(0.05, 0.2))
            cn.tags.append("credit_note:applied")
            out.append(cn)
        return out

    def _near_duplicate_negatives(self, ap: list[Invoice]) -> list[Invoice]:
        """Legit repeat orders: same vendor and amount, new number, a few days later."""
        out = []
        pool = self._candidates(ap, paid=True)
        for i in range(1, N_NEAR_DUPLICATE_NEGATIVES + 1):
            orig = self._pick(pool)
            pool.remove(orig)
            v = next(x for x in self.vendors if x.id == orig.vendor_id)
            again = self._copy(orig, number=next_number(v, orig.issue_date),
                               issue_date=orig.issue_date + timedelta(days=int(self.rng.integers(2, 11))))
            again.issue_date = next_business_day(again.issue_date)
            again.due_date = again.issue_date + (orig.due_date - orig.issue_date)
            again.format = orig.format
            again.tags.append(_tag("negdup", f"negdup-{i:02d}", "legit_repeat"))
            orig.tags.append(_tag("negdup", f"negdup-{i:02d}", "original"))
            out.append(again)
        return out

    # -- fraud

    def _fraud(self) -> list[Invoice]:
        end = self.cfg.end
        out = []

        # 1. Look-alike email announces new bank details, then an invoice with the new IBAN.
        v = self.by_name["Verrerie Industrielle de l'Ain"]
        pid, iban = "fraud-01", foreign_iban(self.rng, "LT")
        inv = self._fraud_invoice(v, end - timedelta(days=6), end + timedelta(days=5), iban, "pdf",
                                  target=11800)
        inv.tags.append(_tag("fraud", pid, "invoice"))
        out.append(inv)
        self.meta[pid] = {"kind": "fraud", "type": "bank_change_email", "vendor_id": v.id,
                          "fraud_iban": iban, "iban_on_file": v.iban}
        self.planned_emails.append({
            "kind": "fraud_bank_change", "plant_id": pid, "vendor": v, "date": end - timedelta(days=12),
            "new_iban": iban, "from_domain": lookalike_domain(v.email_domain, self.rng),
        })

        # 2. Brand-new vendor, invalid SIREN, urgent "CEO" email, very short terms.
        pid = "fraud-02"
        valid = siren(self.rng)
        fake_siren = valid[:-1] + str((int(valid[-1]) + 1) % 10)  # break the Luhn check digit
        nv = Vendor(
            id=f"V{len(self.vendors) + 1:03d}", name="Nexora Conseil SAS", siren=fake_siren,
            vat_number=fr_vat(fake_siren), iban=foreign_iban(self.rng, "DE"), bic="NXRADEFFXXX",
            phone="+33 7 56 12 98 40", email_domain="nexora-conseil.fr",
            address=Address(street="58 avenue de la Grande Armée", postcode="75017", city="Paris"),
            category="services", size="micro", payment_terms_days=3, pay_method="transfer",
            number_pattern="NX-{yyyy}-{n3}", next_seq=17, template=1,
            products=[Product(name="Mission de conseil stratégique - phase 1", unit_price=Decimal("12400"),
                              vat_rate=Decimal("20"))],
            cadence="variable", created_on=end - timedelta(days=9),
        )
        self.vendors.append(nv)
        rec = master_record(nv, f"VR{len(self.records) + 1:03d}", end - timedelta(days=9))
        self.records.append(rec)
        self.main_record[nv.id] = rec
        inv = self._fraud_invoice(nv, end - timedelta(days=8), end + timedelta(days=3), nv.iban, "pdf",
                                  target=12400)
        inv.tags.append(_tag("fraud", pid, "invoice"))
        out.append(inv)
        self.meta[pid] = {"kind": "fraud", "type": "new_vendor_urgent", "vendor_id": nv.id,
                          "fraud_iban": nv.iban, "iban_on_file": nv.iban, "siren_valid": False}
        self.planned_emails.append({
            "kind": "fraud_ceo", "plant_id": pid, "vendor": nv, "date": end - timedelta(days=7),
            "from_domain": lookalike_domain(self.company.email_domain, self.rng),
        })

        # 3. Tampered scanned invoice from a real vendor: IBAN swapped, terms shortened, no email.
        v = self.by_name["Plastiques Moulés Oyonnax"]
        pid, iban = "fraud-03", fr_iban(self.rng, bank_code="16958")
        inv = self._fraud_invoice(v, end - timedelta(days=5), end + timedelta(days=9), iban, "scan",
                                  target=9600)
        inv.tags.append(_tag("fraud", pid, "invoice"))
        out.append(inv)
        self.meta[pid] = {"kind": "fraud", "type": "tampered_invoice_iban", "vendor_id": v.id,
                          "fraud_iban": iban, "iban_on_file": v.iban}
        return out

    def _fraud_invoice(self, v: Vendor, issue: date, due: date, iban: str, fmt: str,
                       target: float) -> Invoice:
        issue = next_business_day(issue)
        n_lines = min(2, len(v.products))
        items = []
        for p in v.products[:n_lines]:
            qty = max(1, round(target / n_lines / float(p.unit_price)))
            items.append((p.name, Decimal(qty), p.unit_price, p.vat_rate))
        inv = ap_invoice(self.ids, v, self.main_record[v.id], self.company, issue, build_lines(items),
                         self.rng, fmt=fmt, iban=iban)
        inv.due_date = due
        inv.payable = False
        return inv
