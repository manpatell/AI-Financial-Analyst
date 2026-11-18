"""Checksum-valid French identifiers: SIREN (Luhn), intra-EU VAT, RIB/IBAN (mod 97)."""

from __future__ import annotations

import numpy as np


def _luhn_check_digit(digits: str) -> int:
    total = 0
    # Positions counted from the right once the check digit is appended: double every second.
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def luhn_valid(number: str) -> bool:
    return _luhn_check_digit(number[:-1]) == int(number[-1])


def siren(rng: np.random.Generator) -> str:
    body = "".join(str(d) for d in rng.integers(0, 10, 8))
    if body[0] == "0":
        body = "1" + body[1:]
    return body + str(_luhn_check_digit(body))


def fr_vat(siren_number: str) -> str:
    key = (12 + 3 * (int(siren_number) % 97)) % 97
    return f"FR{key:02d}{siren_number}"


def _iban_check(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return f"{98 - int(numeric) % 97:02d}"


def iban_valid(iban: str) -> bool:
    s = iban.replace(" ", "")
    numeric = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(numeric) % 97 == 1


def fr_iban(rng: np.random.Generator, bank_code: str | None = None) -> str:
    bank = bank_code or f"{int(rng.integers(10000, 40000)):05d}"
    branch = f"{int(rng.integers(0, 100000)):05d}"
    account = "".join(str(d) for d in rng.integers(0, 10, 11))
    rib_key = 97 - (89 * int(bank) + 15 * int(branch) + 3 * int(account)) % 97
    bban = f"{bank}{branch}{account}{rib_key:02d}"
    return f"FR{_iban_check('FR', bban)}{bban}"


def foreign_iban(rng: np.random.Generator, country: str) -> str:
    """Valid-checksum IBAN for a few countries (IE, LT, GB, DE)."""
    if country == "IE":
        bban = "AIBK" + "".join(str(d) for d in rng.integers(0, 10, 14))
    elif country == "LT":
        bban = "".join(str(d) for d in rng.integers(0, 10, 16))
    elif country == "GB":
        bban = "NWBK" + "".join(str(d) for d in rng.integers(0, 10, 14))
    elif country == "DE":
        bban = "".join(str(d) for d in rng.integers(0, 10, 18))
    else:
        raise ValueError(country)
    return f"{country}{_iban_check(country, bban)}{bban}"


def format_iban(iban: str) -> str:
    return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
