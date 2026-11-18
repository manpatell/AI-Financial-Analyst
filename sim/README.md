# ledgerline-sim

Generates a realistic, fully fictional French SME (*Ateliers Calvet SAS*, an office-furniture maker in
Lyon) with a year of books, then plants known finance problems and writes an answer key, so every
detector, matcher and forecast can be scored.

```bash
ledgerline-sim generate --out data/acme --seed 42 [--start 2025-11-01 --months 12 --invoices 2000]
```

The same seed gives the same data; rendered PDFs differ only in their embedded timestamps.

## What you get

| Path | Contents |
|---|---|
| `invoices/ap/`, `invoices/ar/` | Supplier and customer documents: Factur-X PDF (EN 16931, XSD-valid), UBL 2.1 / CII XML, plain PDF in 3 layouts (FR/EN, varied date and number formats), degraded scans |
| `bank/statement.csv` | French bank export (`;`, decimal comma, debit/credit columns) |
| `bank/camt053/YYYY-MM.xml` | ISO 20022 CAMT.053 statements with opening/closing balances |
| `vendor_master.csv`, `customers.csv` | ERP master data, including messy duplicate vendor rows |
| `emails/*.eml` | AP mailbox: reminders, portal notices, newsletters, planted fraud |
| `saas_usage.csv` | Monthly seats paid vs active users |
| `payment_run_proposed.csv` | Open supplier invoices due within 14 days of period end |
| `answer_key.json`, `ground_truth/` | The answers. Products under test must not read these. |

## Realism

- 41 vendors in 10 categories (materials, logistics, SaaS, utilities, rent…), 15 customers
- Seasonality (August is dead), headcount growth, payroll, URSSAF, monthly VAT, a loan, bank fees, a grant
- E-invoicing share rises after the 1 Sep 2026 French reform for large and mid-size suppliers
- Payments: transfers, direct debits, card payments in USD with FX margin, batched supplier payments,
  credit notes netted on payment, customer partial payments and one-to-many transfers, payment
  references that are missing, vague or contain typos
- Checksum-valid SIREN, VAT and IBAN numbers (except where invalid on purpose)

## Planted problems (seed 42)

| Kind | Count | Detail |
|---|---|---|
| Duplicate payments | 25 | exact, changed reference (`INV-5521` vs `5521-OPS`), two channels (Factur-X + scan), vendor saved twice, credit note never applied. 21 already paid (recoverable), 4 still open (at risk) |
| Fraud | 3 | look-alike-domain bank-change email + new IBAN; brand-new vendor with invalid SIREN pushed by a fake-CEO email; tampered scan with a swapped IBAN |
| Vendor saved twice | 5 clusters | name variants, missing SIREN, address/phone variants |
| Price creep | 2 vendors | silent monthly unit-price increases |
| Unused SaaS | 3 | seats paid, zero active users |
| Late payers | 3 customers | average 40–58 days past due |

**Negatives** (to measure precision): legitimate repeat orders with identical amounts, 10 applied credit
notes, a genuine bank-detail change with a proper email, and a similarly named but different vendor.
