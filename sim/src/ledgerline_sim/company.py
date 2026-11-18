"""Builds the company, its vendors (true entities + ERP vendor-master rows) and customers."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from faker import Faker

from . import catalog
from .ids import foreign_iban, fr_iban, fr_vat, siren
from .models import Address, Company, Customer, Product, Vendor, VendorRecord, money

# Fixed-date French public holidays (movable ones are ignored; close enough for a simulator).
_HOLIDAYS = {(1, 1), (5, 1), (5, 8), (7, 14), (8, 15), (11, 1), (11, 11), (12, 25)}

# Monthly activity multipliers (Jan..Dec) for an office-furniture maker: August is dead.
SEASONALITY = [0.9, 1.0, 1.15, 1.1, 1.05, 1.1, 0.85, 0.45, 1.2, 1.15, 1.05, 0.8]


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and (d.month, d.day) not in _HOLIDAYS


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def slug(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"\b(sas|sarl|sa|inc|ltd|et|&|de|du|des|l|d)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def bic_for(name: str, country: str = "FR") -> str:
    letters = re.sub(r"[^A-Z]", "", strip_accents(name).upper()) + "XXXX"
    return f"{letters[:4]}{country}PP"


_FOREIGN = {"US": Faker("en_US"), "IE": Faker("en_IE")}


def _address(fake: Faker, city: str | None = None, country: str = "FR") -> Address:
    if country != "FR":
        f = _FOREIGN[country]
        f.seed_instance(int(fake.random_int(0, 10**9)))
        return Address(street=f.street_address(), postcode=f.postcode(), city=city or f.city(),
                       country=country)
    return Address(street=fake.street_address(), postcode=fake.postcode(), city=city or fake.city())


def build_company(rng: np.random.Generator, fake: Faker, opening_balance: Decimal) -> Company:
    c = catalog.COMPANY
    s = siren(rng)
    iban = fr_iban(rng, bank_code="30004")
    return Company(
        id="COMPANY", name=c["name"], siren=s, vat_number=fr_vat(s), iban=iban,
        bic="BNPAFRPPXXX", phone=c["phone"], email_domain=c["email_domain"],
        address=Address(street=c["street"], postcode=c["postcode"], city=c["city"]),
        opening_balance=opening_balance, bank_account_iban=iban,
    )


def build_vendors(rng: np.random.Generator, fake: Faker) -> list[Vendor]:
    vendors = []
    for i, spec in enumerate(catalog.VENDOR_SPECS, start=1):
        country = spec.get("country", "FR")
        french = country == "FR"
        s = siren(rng) if french else None
        iban = fr_iban(rng) if french else foreign_iban(rng, "IE" if country == "IE" else "GB")
        tld = {"FR": "fr", "IE": "ie", "US": "com"}[country]
        pattern = catalog.NUMBER_PATTERNS[int(rng.integers(len(catalog.NUMBER_PATTERNS)))]
        vendors.append(Vendor(
            id=f"V{i:03d}", name=spec["name"], siren=s,
            vat_number=fr_vat(s) if s else (f"IE{int(rng.integers(1000000, 9999999))}W" if country == "IE"
                                            else None),
            iban=iban, bic=bic_for(spec["name"], country if country != "US" else "GB"),
            phone=fake.phone_number() if french else f"+1 415 {int(rng.integers(200, 999))} "
                                                     f"{int(rng.integers(1000, 9999))}",
            email_domain=f"{slug(spec['name'])}.{tld}",
            address=_address(fake, country=country),
            category=spec["category"], size=spec["size"], currency=spec.get("currency", "EUR"),
            payment_terms_days=spec["terms"], pay_method=spec["pay_method"],
            number_pattern=pattern, next_seq=int(rng.integers(100, 6000)),
            template=int(rng.integers(3)),
            products=[Product(name=n, unit_price=Decimal(str(p)), vat_rate=Decimal(v))
                      for n, p, v in spec["products"]],
            cadence=spec["cadence"], weight=spec.get("weight", 0.0), seats=spec.get("seats", 0),
            issue_day=0 if spec["category"] == "rent" else int(rng.integers(0, 20)),
        ))
    return vendors


def master_record(v: Vendor, record_id: str, created_on: date, **overrides) -> VendorRecord:
    data = dict(
        record_id=record_id, vendor_id=v.id, name=v.name, siren=v.siren, vat_number=v.vat_number,
        iban=v.iban, phone=v.phone, email=f"compta@{v.email_domain}", address=v.address,
        created_on=created_on,
    )
    data.update(overrides)
    return VendorRecord(**data)


def build_vendor_master(vendors: list[Vendor], start: date, rng: np.random.Generator) -> list[VendorRecord]:
    records = []
    for v in vendors:
        created = start - timedelta(days=int(rng.integers(120, 2500)))
        records.append(master_record(v, f"VR{v.id[1:]}", created))
    return records


def build_customers(rng: np.random.Generator, fake: Faker) -> list[Customer]:
    customers = []
    for i, spec in enumerate(catalog.CUSTOMER_SPECS, start=1):
        s = siren(rng)
        customers.append(Customer(
            id=f"C{i:03d}", name=spec["name"], siren=s, vat_number=fr_vat(s), iban=fr_iban(rng),
            bic=bic_for(spec["name"]), phone=fake.phone_number(),
            email_domain=f"{slug(spec['name'])}.fr", address=_address(fake, city=spec["city"]),
            payment_terms_days=spec["terms"], mean_delay_days=spec["delay"], sd_delay_days=spec["sd"],
            pays_by_statement=spec.get("statement", False), weight=spec["weight"],
        ))
    return customers


def scale_price(p: Decimal, factor: float) -> Decimal:
    return money(p * Decimal(str(factor)))
