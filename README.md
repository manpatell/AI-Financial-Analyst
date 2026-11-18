# Ledgerline

An AI finance analyst that finds money and never makes up a number. See
[the project plan](ai-finance-analyst-project-plan.html) and the [build plan](BUILD_PLAN.md).

Status: **phase 1 (foundations + simulator) done.**

## Layout

```
sim/       ledgerline-sim: synthetic French SME with planted problems + answer key (standalone package)
backend/   ledgerline: ledger schema (Postgres), migrations; ingestion/agent come in later phases
evals/     evaluation harnesses (phase 2+)
web/       Next.js dashboard (phase 6)
data/      generated companies (git-ignored)
```

## Setup

Needs Python 3.12 + [uv](https://docs.astral.sh/uv/), and Docker (or Podman) for Postgres.

```bash
uv sync                      # add --system-certs if your network intercepts TLS
cp .env.example .env
docker compose up -d postgres   # Postgres 16 on localhost:5433 (dev-only credentials)
cd backend && uv run alembic upgrade head && cd ..
```

## Generate a company

```bash
uv run ledgerline-sim generate --out data/acme --seed 42          # ~1 min with rendering
uv run ledgerline-sim generate --out data/fast --no-render         # seconds; ground truth only
```

One run produces about 2,000 invoices (Factur-X, UBL, CII, PDF, noisy scans), 12 months of bank
statements (CSV + CAMT.053), a vendor master, emails and an `answer_key.json`. See
[sim/README.md](sim/README.md) for what is planted.

## Tests

```bash
uv run pytest            # simulator + ledger (ledger tests skip if Postgres is down)
uv run ruff check .
```
