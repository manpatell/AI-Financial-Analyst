"""Monthly SaaS seat usage, as an SSO / admin-console export would show it."""

from __future__ import annotations

import numpy as np

from .config import SimConfig
from .models import UsageRow, Vendor
from .plant import UNUSED_SUBSCRIPTIONS


def build_usage(cfg: SimConfig, rng: np.random.Generator, vendors: list[Vendor]) -> list[UsageRow]:
    unused_from = dict(UNUSED_SUBSCRIPTIONS)
    rows = []
    for v in vendors:
        if v.category != "saas":
            continue
        for mi, ms in enumerate(cfg.month_starts):
            seats = cfg.headcount(ms) if v.seats == 0 or "Paie" in v.name else v.seats
            if v.name in unused_from and mi >= unused_from[v.name]:
                active = 1 if mi == unused_from[v.name] and rng.random() < 0.5 else 0
            else:
                active = max(1, int(round(seats * rng.uniform(0.6, 1.0))))
            rows.append(UsageRow(month=f"{ms:%Y-%m}", tool=v.name, vendor_id=v.id, seats_paid=seats,
                                 active_users=active))
    return rows
