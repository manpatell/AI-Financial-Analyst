from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import typer

from .config import SimConfig
from .export import write_world
from .world import generate

app = typer.Typer(add_completion=False, help="Generate a synthetic French SME with planted finance problems.")


@app.command("generate")
def generate_cmd(
    out: Path = typer.Option(Path("data/acme"), help="Output folder."),
    seed: int = typer.Option(42, help="Random seed; same seed gives the same company."),
    start: str = typer.Option("2025-11-01", help="First day of the simulated period (YYYY-MM-DD)."),
    months: int = typer.Option(12, min=6, help="Number of months to simulate."),
    invoices: int = typer.Option(2000, min=800, help="Approximate total number of invoices."),
    opening_balance: str = typer.Option("700000.00", help="Bank balance at the start (EUR)."),
    render: bool = typer.Option(True, help="Render invoice files (PDF/XML). --no-render is much faster."),
    workers: int = typer.Option(0, help="Render processes (0 = CPU count - 1)."),
) -> None:
    """Generate one company: documents, bank statements, emails and the answer key."""
    t0 = time.perf_counter()
    cfg = SimConfig(seed=seed, start=date.fromisoformat(start), months=months, n_invoices=invoices,
                    opening_balance=Decimal(opening_balance))
    world = generate(cfg)
    t1 = time.perf_counter()
    write_world(world, out, render=render, workers=workers or None)
    t2 = time.perf_counter()
    typer.echo(json.dumps(world.answer_key["summary"], indent=2))
    typer.echo(f"simulated in {t1 - t0:.1f}s, written in {t2 - t1:.1f}s -> {out}")


@app.command()
def version() -> None:
    """Print the package version."""
    from importlib.metadata import version as v

    typer.echo(v("ledgerline-sim"))


if __name__ == "__main__":
    app()
