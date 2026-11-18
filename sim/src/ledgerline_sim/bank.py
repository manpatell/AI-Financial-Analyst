"""Turns invoices into bank activity (and the ground-truth matches), plus non-invoice flows."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import numpy as np

from .company import next_business_day, prev_business_day, strip_accents
from .config import SimConfig
from .models import BankTxn, Company, Customer, Invoice, Match, Vendor, money


def bank_name(name: str, rng: np.random.Generator | None = None) -> str:
    """How a counterparty name shows on a French statement: upper, no accents, truncated."""
    s = strip_accents(name).upper().replace("&", "ET").replace("'", " ")
    if rng is not None and rng.random() < 0.4:
        s = re.sub(r"\b(SAS|SARL|SA|INC\.?|LTD)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()[:32]


class FxRates:
    """Daily EUR/USD random walk."""

    def __init__(self, cfg: SimConfig, rng: np.random.Generator):
        n = (cfg.end - cfg.start).days + 30
        steps = rng.normal(0, 0.003, n)
        self._start = cfg.start
        self._rates = 1.16 * np.exp(np.cumsum(steps))

    def eurusd(self, d: date) -> Decimal:
        return Decimal(str(round(float(self._rates[(d - self._start).days]), 4)))


class BankBook:
    def __init__(self, cfg: SimConfig, rng: np.random.Generator, company: Company):
        self.cfg, self.rng, self.company = cfg, rng, company
        self.txns: list[BankTxn] = []
        self.matches: list[Match] = []
        self._n = 0

    def add(self, d: date, amount: Decimal, name: str, iban: str | None, ref: str, code: str,
            category: str, allocations: list[tuple[str, Decimal]] = (), **extra) -> BankTxn | None:
        if d > self.cfg.end:
            return None
        self._n += 1
        t = BankTxn(txn_id=f"tmp-TXN-{self._n}", booking_date=d, value_date=extra.pop("value_date", d),
                    amount=money(amount), counterparty_name=name, counterparty_iban=iban,
                    reference=ref[:140], bank_code=code, category=category, **extra)
        self.txns.append(t)
        for doc_id, alloc in allocations:
            self.matches.append(Match(txn_id=t.txn_id, doc_id=doc_id, allocated_amount=money(alloc)))
        return t

    # ------------------------------------------------------------ AP

    def pay_suppliers(self, ap: list[Invoice], vendors: dict[str, Vendor], fx: FxRates) -> None:
        rng = self.rng
        credits: dict[str, list[Invoice]] = defaultdict(list)
        for inv in ap:
            if inv.doc_type == "credit_note" and "credit_note:applied" in inv.tags:
                credits[inv.references_invoice].append(inv)

        transfers: dict[str, list[tuple[date, Invoice]]] = defaultdict(list)
        for inv in ap:
            if inv.doc_type != "invoice" or any(t.startswith("fraud:") for t in inv.tags):
                continue
            v = vendors[inv.vendor_id]
            if v.pay_method == "card":
                d = next_business_day(inv.issue_date + timedelta(days=int(rng.integers(0, 2))))
                if inv.currency == "USD":
                    rate = fx.eurusd(d)
                    eur = money(inv.total / rate * Decimal("1.0125"))  # bank FX margin
                    self.add(d, -eur, f"CB {bank_name(v.name)}", None,
                             f"CB {bank_name(v.name)[:20]} {inv.total} USD {d:%d/%m}", "CB", "supplier",
                             [(inv.doc_id, eur)], original_amount=inv.total, original_currency="USD")
                else:
                    self.add(d, -inv.total, f"CB {bank_name(v.name)}", None,
                             f"CB {bank_name(v.name)[:20]} {d:%d/%m}", "CB", "supplier",
                             [(inv.doc_id, inv.total)])
            elif v.pay_method == "direct_debit":
                d = next_business_day(inv.due_date)
                ref = (f"PRLV SEPA {bank_name(v.name)} {inv.number}" if rng.random() < 0.6
                       else f"PRLV SEPA {bank_name(v.name)} ECH {inv.issue_date:%m/%Y}")
                self.add(d, -inv.total, bank_name(v.name), inv.iban, ref, "PRLV", "supplier",
                         [(inv.doc_id, inv.total)])
            else:
                d = next_business_day(max(inv.issue_date + timedelta(days=1),
                                          inv.due_date + timedelta(days=int(rng.integers(-3, 6)))))
                transfers[v.id].append((d, inv))

        for vid, items in transfers.items():
            v = vendors[vid]
            by_week: dict[tuple, list[tuple[date, Invoice]]] = defaultdict(list)
            for d, inv in items:
                batchable = not inv.tags and inv.iban == v.iban
                key = (d.isocalendar()[:2], inv.iban) if batchable else (id(inv),)
                by_week[key].append((d, inv))
            for group in by_week.values():
                if len(group) > 1 and rng.random() < 0.35:
                    self._transfer(v, [inv for _, inv in group], max(d for d, _ in group), credits)
                else:
                    for d, inv in group:
                        self._transfer(v, [inv], d, credits)

    def _transfer(self, v: Vendor, invs: list[Invoice], d: date, credits: dict) -> None:
        rng = self.rng
        allocs: list[tuple[str, Decimal]] = []
        for inv in invs:
            allocs.append((inv.doc_id, inv.total))
            allocs += [(cn.doc_id, -cn.total) for cn in credits.get(inv.doc_id, [])]
        amount = sum((a for _, a in allocs), Decimal("0"))
        short = bank_name(v.name)[:18]
        if len(invs) > 1:
            ref = (f"REGLEMENT FACTURES {' '.join(i.number for i in invs)}" if rng.random() < 0.7
                   else f"REGLEMENT {short} {d:%m/%Y}")
        else:
            n = invs[0].number
            options = [f"FACT {n}", n, f"VIR {short} {n}", f"FACTURE {n} {short}", f"REGLEMENT {short}"]
            ref = options[int(rng.choice(len(options), p=[0.3, 0.25, 0.2, 0.15, 0.1]))]
        self.add(d, -amount, bank_name(v.name, rng), invs[0].iban, ref, "VIR", "supplier", allocs)

    # ------------------------------------------------------------ AR

    def collect_customers(self, ar: list[Invoice], customers: dict[str, Customer]) -> None:
        rng = self.rng
        by_customer: dict[str, list[Invoice]] = defaultdict(list)
        for inv in ar:
            by_customer[inv.customer_id].append(inv)

        for cid, invs in by_customer.items():
            c = customers[cid]
            name = bank_name(c.name, rng)
            if c.pays_by_statement:
                by_month: dict[tuple, list[Invoice]] = defaultdict(list)
                for inv in invs:
                    by_month[(inv.issue_date.year, inv.issue_date.month)].append(inv)
                for (y, m), group in by_month.items():
                    nm = date(y + (m == 12), m % 12 + 1, 1)
                    base = nm + timedelta(days=c.payment_terms_days - 21)
                    d = next_business_day(base + timedelta(days=int(rng.normal(c.mean_delay_days,
                                                                                c.sd_delay_days))))
                    total = sum((i.total for i in group), Decimal("0"))
                    ref = (f"REGLEMENT RELEVE {m:02d}/{y}" if rng.random() < 0.5
                           else "REGLT " + " ".join(i.number for i in group))
                    self.add(d, total, name, c.iban, ref, "VIR_RECU", "customer",
                             [(i.doc_id, i.total) for i in group])
                continue

            scheduled = []
            for inv in invs:
                delay = int(round(rng.normal(c.mean_delay_days, c.sd_delay_days)))
                d = next_business_day(max(inv.issue_date + timedelta(days=2),
                                          inv.due_date + timedelta(days=delay)))
                scheduled.append((d, inv))
            scheduled.sort(key=lambda x: x[0])

            i = 0
            while i < len(scheduled):
                d, inv = scheduled[i]
                if (i + 1 < len(scheduled) and (scheduled[i + 1][0] - d).days <= 7
                        and rng.random() < 0.2):  # pays two invoices with one transfer
                    d2, inv2 = scheduled[i + 1]
                    self.add(d2, inv.total + inv2.total, name, c.iban,
                             self._ar_ref([inv, inv2]), "VIR_RECU", "customer",
                             [(inv.doc_id, inv.total), (inv2.doc_id, inv2.total)])
                    i += 2
                    continue
                if rng.random() < 0.06:  # partial payment: deposit then balance
                    first = money(inv.total * Decimal(str(round(rng.uniform(0.3, 0.6), 2))))
                    self.add(d, first, name, c.iban, f"ACOMPTE FACT {inv.number}", "VIR_RECU",
                             "customer", [(inv.doc_id, first)])
                    d2 = next_business_day(d + timedelta(days=int(rng.integers(15, 40))))
                    self.add(d2, inv.total - first, name, c.iban, f"SOLDE FACT {inv.number}",
                             "VIR_RECU", "customer", [(inv.doc_id, inv.total - first)])
                else:
                    self.add(d, inv.total, name, c.iban, self._ar_ref([inv]), "VIR_RECU", "customer",
                             [(inv.doc_id, inv.total)])
                i += 1

    def _ar_ref(self, invs: list[Invoice]) -> str:
        rng = self.rng
        nums = [i.number for i in invs]
        if rng.random() < 0.05:  # typo: two digits swapped
            n = list(nums[0])
            k = max(i for i, ch in enumerate(n) if ch.isdigit())
            n[k], n[k - 1] = n[k - 1], n[k]
            nums[0] = "".join(n)
        options = [" ".join(nums), "FACTURE " + " ".join(nums), "PAIEMENT FACT " + " ".join(nums),
                   "VIREMENT", f"REGLEMENT {_MOIS[invs[0].issue_date.month - 1]}", ""]
        return options[int(rng.choice(len(options), p=[0.3, 0.25, 0.2, 0.1, 0.1, 0.05]))]

    # ------------------------------------------------------------ other flows

    def operating_flows(self, ap: list[Invoice], ar: list[Invoice]) -> None:
        cfg, rng = self.cfg, self.rng
        vat_in: dict[tuple, Decimal] = defaultdict(Decimal)
        vat_out: dict[tuple, Decimal] = defaultdict(Decimal)
        for inv in ap:
            if inv.currency == "EUR" and not any(t.startswith("fraud:") for t in inv.tags):
                vat_in[(inv.issue_date.year, inv.issue_date.month)] += (
                    -inv.vat if inv.doc_type == "credit_note" else inv.vat)
        for inv in ar:
            vat_out[(inv.issue_date.year, inv.issue_date.month)] += inv.vat

        months = cfg.month_starts + [date(cfg.end.year + (cfg.end.month == 12), cfg.end.month % 12 + 1, 1)]
        for prev, ms in zip([None] + months[:-1], months, strict=True):
            hc = cfg.headcount(ms)
            if ms <= cfg.end:
                # Payroll for month ms, paid around the 28th.
                net_pay = money(hc * 2680 * rng.uniform(0.98, 1.03))
                self.add(prev_business_day(ms.replace(day=28)), -net_pay, "VIREMENTS SALAIRES", None,
                         f"VIR SALAIRES {ms:%m/%Y} LOT {hc} BENEFICIAIRES", "VIR", "payroll")
                self.add(next_business_day(ms + timedelta(days=4)), Decimal("-4280.17"), "BPIFRANCE",
                         None, "ECHEANCE PRET 0042871 BPI", "ECH", "loan")
                self.add(prev_business_day(_month_end(ms)), Decimal("-38.00"), "BANQUE", None,
                         "FRAIS TENUE DE COMPTE", "FRAIS", "bank_fees")
                self.add(prev_business_day(_month_end(ms)), -money(rng.uniform(18, 46)), "BANQUE", None,
                         "COMMISSIONS MOUVEMENTS", "FRAIS", "bank_fees")
            if prev is None:
                continue
            # Social charges and withholding tax for the previous month, on the 15th.
            d15 = next_business_day(ms.replace(day=15))
            hc_prev = cfg.headcount(prev)
            self.add(d15, -money(hc_prev * 1790 * rng.uniform(0.98, 1.02)), "URSSAF RHONE-ALPES",
                     None, f"PRLV SEPA URSSAF COTISATIONS {prev:%m/%Y}", "PRLV",
                     "social_charges")
            self.add(d15, -money(hc_prev * 205 * rng.uniform(0.95, 1.05)), "DGFIP", None,
                     f"PRLV SEPA DGFIP PAS {prev:%m/%Y}", "PRLV", "withholding_tax")
            # Monthly VAT return: output VAT minus deductible VAT, paid around the 19th.
            due_vat = vat_out[(prev.year, prev.month)] - vat_in[(prev.year, prev.month)]
            if due_vat > 0:
                self.add(next_business_day(ms.replace(day=19)), -due_vat, "DGFIP", None,
                         f"PRLV SEPA DGFIP TVA {prev:%m/%Y}", "PRLV", "vat")

        grant_day = next_business_day(cfg.month_starts[4] + timedelta(days=12))
        self.add(grant_day, Decimal("45000.00"), "BPIFRANCE FINANCEMENT", None,
                 "VERSEMENT AIDE INNOVATION DOSSIER 2025-AI-0317", "VIR_RECU", "grant")

    def finalize(self) -> dict[str, str]:
        """Assign final txn ids in booking order; returns tmp->final id map."""
        self.txns.sort(key=lambda t: (t.booking_date, t.txn_id.split("-")[-1].zfill(8)))
        mapping = {t.txn_id: f"TXN-{i:06d}" for i, t in enumerate(self.txns, start=1)}
        for t in self.txns:
            t.txn_id = mapping[t.txn_id]
        for m in self.matches:
            m.txn_id = mapping[m.txn_id]
        return mapping

    def balance_series(self) -> list[tuple[date, Decimal]]:
        bal = self.company.opening_balance
        out = []
        for t in self.txns:
            bal += t.amount
            out.append((t.booking_date, bal))
        return out


_MOIS = ["JANVIER", "FEVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOUT", "SEPTEMBRE",
         "OCTOBRE", "NOVEMBRE", "DECEMBRE"]


def _month_end(ms: date) -> date:
    nxt = date(ms.year + (ms.month == 12), ms.month % 12 + 1, 1)
    return nxt - timedelta(days=1)
