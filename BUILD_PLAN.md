# Ledgerline — Build Plan

Engineering breakdown of `ai-finance-analyst-project-plan.html`. The product plan says *what* and *why*; this file says *what to build, in what order, and how to know it's done*.

Budget: 6 weeks × 15–20 h. Must-haves: features 1–3, 5–9. Stretch: 4, 10–13 (cut these before slipping launch).

---

## 0. Decisions locked before writing code

| Decision | Choice | Reason |
|---|---|---|
| Repo shape | Monorepo: `backend/` (Python, uv), `web/` (Next.js), `sim/` (simulator package) | Simulator ships as its own package later |
| Python | 3.12 via `uv` (3.13 is fine too, but LightGBM/Splink/MAPIE wheels are safest on 3.12) | Avoid wheel-build pain on Windows |
| Database | Postgres 16 in Docker Compose for the ledger; DuckDB for analytics/forecast/eval | Matches the plan. **Install Docker Desktop first (not on this machine yet).** |
| Fallback if no Docker | SQLAlchemy models + Alembic, test suite runs on SQLite | Keeps week 1 unblocked |
| Money type | `Decimal` in Python, `NUMERIC(14,2)` in SQL, amounts stored in cents-safe form. Never float. | Provenance validator compares numbers exactly |
| LLM | Anthropic API: `claude-haiku-5-5` = small route, `claude-sonnet-5-5` = frontier route. Wrap behind one `llm/` interface so the eval can compare others | Cost routing (feature 11) needs ≥2 tiers |
| Agent framework | Plain tool-calling loop (no LangGraph in v1) | Fewer moving parts; easier provenance control |
| Provenance ID format | `<table>:<pk>[:<field>]`, e.g. `inv:1042:total`, `txn:88`, `calc:7f3a` (calc = stored tool output with its inputs) | Every number in an answer must resolve to one of these |
| Seeded randomness | Simulator takes `--seed`; answer key is deterministic per seed | Reproducible evals in CI |

---

## 1. Repo layout

```
ledgerline/
├── docker-compose.yml          # postgres, backend, web
├── policy.yaml                 # feature 8
├── sim/                        # feature 9 — standalone package "ledgerline-sim"
│   ├── company.py              # vendors, customers, seasonality
│   ├── invoices.py             # invoice + line generation
│   ├── bank.py                 # transactions, CSV + CAMT.053 export
│   ├── emails.py               # .eml incl. "bank details changed"
│   ├── render/                 # facturx.py, ubl.py, scan.py (noisy PDF)
│   ├── plant.py                # duplicates, fraud, price creep, late payers…
│   └── answer_key.py           # JSON of every planted issue
├── backend/
│   ├── ledger/                 # SQLAlchemy models, Alembic migrations, audit log
│   ├── ingest/                 # xml_parser.py, vision_extract.py, bank_parser.py, email_signals.py, router.py
│   ├── resolve/                # vendor entity resolution (Splink)
│   ├── duplicates/             # normalise, candidate pairs, LightGBM scorer
│   ├── fraud/                  # IBAN history, velocity, email risk
│   ├── recon/                  # rules → ML → LLM matcher, exception queue
│   ├── forecast/               # 13-week, conformal bands, scenarios
│   ├── policy/                 # policy.yaml engine, approval queue
│   ├── agent/                  # orchestrator, tools, provenance validator
│   ├── mcp_server/             # feature 12
│   ├── reports/                # monthly pack
│   ├── api/                    # FastAPI routers
│   └── llm/                    # provider wrapper + response cache
├── evals/                      # extraction, detection, recon, forecast, agent (500 Qs)
├── web/                        # Next.js + Tailwind + Recharts
└── .github/workflows/          # tests + evals on merge
```

---

## 2. Ledger schema (week 1, day 1 — everything depends on it)

- `vendors` (id, name, siren, vat, iban, phone, address, canonical_vendor_id)
- `customers` (same shape; needed for receivables and the forecast)
- `invoices` (id, direction AP/AR, vendor_id/customer_id, number, issue_date, due_date, currency, net, vat, total, iban_on_invoice, source_format [facturx|ubl|cii|pdf|scan], source_file, extraction_route, confidence_json)
- `invoice_lines` (id, invoice_id, description, qty, unit_price, vat_rate, amount)
- `bank_transactions` (id, account, date, amount, counterparty, counterparty_iban, reference, raw_json)
- `emails` (id, from, subject, received_at, body, risk_signals_json)
- `matches` (id, txn_id, invoice_id, allocated_amount, method [rule|ml|llm|human], confidence, status)
- `findings` (id, kind [duplicate|fraud|price_creep|unused_sub], subject_ids, amount_at_risk, score, evidence_json, status)
- `decisions` **append-only** (id, ts, actor [agent|user:x], action, target_ids, rule_applied, model_version, input_lineage, confidence, approved_by, approved_at). Enforce with a DB trigger that rejects UPDATE/DELETE.
- `calc_outputs` (id, tool, inputs_json, result_json, created_at) — backing store for `calc:` provenance IDs

---

## 3. Week-by-week build order

Each week ends with a demo-able checkpoint and a test or eval that proves it.

### Week 1 — Foundations + simulator
1. Install Docker Desktop; `docker compose up postgres`. Scaffold repo, uv project, ruff, pytest, pre-commit.
2. SQLAlchemy models + first Alembic migration (schema above), append-only trigger + test that it blocks updates.
3. Simulator core: ~40 vendors, ~15 customers, 2,000 invoices, 12 months of bank activity with seasonality and realistic payment delays.
4. `plant.py`: 25 duplicates (exact, changed reference, two channels, unapplied credit note, vendor saved twice), 3 fraud attempts (IBAN change + email), price creep, unused subscriptions, late payers. Each writes to the answer key.
5. Renderers: Factur-X (`factur-x` lib on top of a reportlab PDF), UBL XML, noisy scans (rotate, blur, JPEG artefacts), bank CSV + CAMT.053.
6. CLI: `ledgerline-sim generate --seed 42 --out data/acme/`.

**Done when:** one command produces the full company folder plus `answer_key.json`, and a test asserts counts of planted issues.

### Week 2 — Ingestion, extraction, routing, first eval
1. XML path: Factur-X embedded XML + UBL + CII → Pydantic `ExtractedInvoice` (confidence = 1.0).
2. Vision path: PDF → images → LLM with a JSON schema; per-field confidence from logprob-free heuristics (self-reported + cross-field checks: net + vat = total, line sum = net, date sanity).
3. Router: XML → small model → frontier if any required field is below threshold. Log route + cost per doc.
4. Bank parser (CSV + CAMT.053) and email risk signals (urgency words, "new bank details", sender domain mismatch).
5. LLM response cache (hash of model + prompt + file) so evals don't re-bill.
6. Eval harness: field and line-item accuracy on SROIE, CORD, FATURA subset + 50 of your own French invoices; cost per 1k docs per route.

**Done when:** eval table (accuracy, €/1k docs, by route) is generated into the README.

### Week 3 — Money finders
1. Vendor entity resolution with Splink (blocking on SIREN/VAT/IBAN, comparisons on name + address). Merge suggestions written to `findings`. *(Stretch: review UI — a simple API endpoint is enough for now.)*
2. Duplicate hunter: normalise invoice numbers (strip prefixes/suffixes, digits-only key), candidate pairs by canonical vendor + amount ± tolerance + date window, RapidFuzz features, LightGBM scorer trained on a *different seed* than the one you evaluate on.
3. Fraud guard: IBAN differs from vendor history, new-vendor fast payment, linked risky email → risk score, payment hold, callback checklist using the phone on file.
4. `evals/detection.py`: precision/recall vs answer key, "€ recoverable".

**Done when:** recall ≥ 90% at precision ≥ 90% on duplicates, all 3 frauds caught, "€X recoverable" printed.

### Week 4 — Reconciliation + forecast
1. Recon rules: exact amount + reference contains invoice number; then amount + counterparty + date window.
2. One-to-many (subset-sum over open invoices of the same counterparty, bounded) and partial payments.
3. ML scorer for leftovers; LLM only for unclear references, and its output is a *suggested match ID*, never an amount.
4. Exception queue API.
5. Forecast: known AR/AP due dates shifted by learned per-customer delay + recurring costs (StatsForecast) → 13 weeks; MAPIE / split-conformal 80% bands; runway.
6. Backtest vs naive + seasonal-naive; report band coverage.

**Done when:** auto-match ≥ 95% with < 0.5% wrong, and the forecast beats baselines with 78–82% coverage.

### Week 5 — Agent, provenance, governance
1. Tools (pure functions over SQL/DuckDB, each returning values *with* provenance IDs): `get_cash_position`, `find_duplicates`, `list_exceptions`, `explain_variance`, `run_scenario`, `query_invoices`…
2. Orchestrator loop: LLM chooses tools; answer must cite IDs inline like `€18,400 [calc:7f3a]`.
3. **Provenance validator**: extract every number from the final answer; each must equal a value in a cited tool output (exact after normalising format). On failure, retry once, then refuse. Unit-test it hard.
4. `policy.yaml` engine → auto-act vs approval queue; every action writes a `decisions` row.
5. Scenarios: LLM turns "hire 3 engineers in March" into a typed `ScenarioChange` (Pydantic), code applies it.
6. 500-question eval set generated from the answer key + ledger (templated questions, computed answers). MCP server exposing the read-only tools.

**Done when:** task success ≥ 90% and unverified numbers = 0 across the 500.

### Week 6 — Dashboard, pack, deploy, launch
1. Next.js pages: Overview, Duplicates (side-by-side), Fraud holds, Reconciliation exceptions, Forecast + scenario sliders, Chat, Approvals. Clickable provenance chips open the source doc.
2. Monthly pack: HTML (clickable) + PDF (WeasyPrint or Playwright print).
3. Docker Compose for the full stack; deploy to Fly.io/Render seeded with the synthetic company; "demo on synthetic data, read-only" banner.
4. GitHub Actions: tests on every PR; cached evals on merge to main with a fail threshold.
5. README (architecture diagram, eval tables), 90-second demo video.

**Done when:** live link + public repo + launch post scheduled.

---

## 4. Critical path and risks to watch

- **Simulator realism gates everything.** If week 1 slips, compress week 6 polish, not the simulator.
- **Factur-X rendering** is the most likely time sink. Support the MINIMUM/BASIC/EN16931 profiles only.
- **Data leakage in the duplicate classifier**: train on seed A, evaluate on seed B. Report that.
- **Windows dev**: run Python and Postgres via Docker or WSL2 if Splink/LightGBM wheels cause trouble.
- **Cost**: cache every LLM call; full extraction eval only in CI on merge.

---

## 5. Progress

### Phase 1 (week 1): done, 18 Nov 2026
- [x] uv workspace (`sim/`, `backend/`), ruff, pytest; `git init`
- [x] Postgres 16 via `docker compose` on **port 5433** (a native Postgres 18 already uses 5432)
- [x] Ledger models + Alembic migration 0001; `decisions` and `calc_outputs` append-only via triggers (UPDATE, DELETE, TRUNCATE rejected)
- [x] Simulator: 41 vendors, 15 customers, 2,000 invoices, ~1,600 bank transactions, 47 emails, SaaS usage, proposed payment run
- [x] Planted: 25 duplicates (5 types), 3 frauds, 5 vendor clusters, 2 price creeps, 3 unused subscriptions, 3 late payers, plus negatives
- [x] Renderers: Factur-X (XSD-valid EN 16931), UBL 2.1 + CII (XSD-valid), 3 PDF layouts, noisy scans, bank CSV + CAMT.053
- [x] `ledgerline-sim generate --seed 42` produces everything in about 1 minute; 32 tests pass

Changes from the plan:
- The simulator does **not** load the ledger. Loading documents into the ledger is ingestion (phase 2), so the simulator only produces the files a real company would hand over.
- The company is cash-burning (seed 42: €700k opening balance → €467k closing). Runway depends on the seed; the forecast in phase 4 is what reports it.
- Local environment notes: uv needs `--system-certs` on this network. Docker Desktop is installed per user (`%LOCALAPPDATA%\Programs\DockerDesktop`), so open a new terminal if `docker` isn't on PATH.

### Next: phase 2 (ingestion, extraction, first eval)
- [ ] Loaders: vendor master, customers, bank CSV/CAMT.053, .eml → ledger
- [ ] Factur-X / UBL / CII parser → `ExtractedInvoice` (exact path)
- [ ] Vision-LLM extraction with per-field confidence + cross-field checks; small→frontier routing; response cache
- [ ] Extraction eval vs `ground_truth/invoices.jsonl` (+ SROIE / CORD / FATURA), €/1k documents per route
