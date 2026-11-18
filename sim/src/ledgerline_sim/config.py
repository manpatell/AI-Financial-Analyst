from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# From 1 Sep 2026 large and mid-size French companies must issue structured e-invoices.
EINVOICING_REFORM = date(2026, 9, 1)


@dataclass(frozen=True)
class SimConfig:
    seed: int = 42
    start: date = date(2025, 11, 1)
    months: int = 12
    n_invoices: int = 2000
    opening_balance: Decimal = field(default_factory=lambda: Decimal("700000.00"))
    payment_run_horizon_days: int = 14

    @property
    def month_starts(self) -> list[date]:
        out = []
        y, m = self.start.year, self.start.month
        for _ in range(self.months):
            out.append(date(y, m, 1))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    @property
    def end(self) -> date:
        last = self.month_starts[-1]
        return date(last.year, last.month, calendar.monthrange(last.year, last.month)[1])

    def headcount(self, d: date) -> int:
        """Grows by one hire roughly every quarter."""
        months_in = (d.year - self.start.year) * 12 + d.month - self.start.month
        return 22 + max(0, months_in) // 3

    def month_index(self, d: date) -> int:
        return (d.year - self.start.year) * 12 + d.month - self.start.month
