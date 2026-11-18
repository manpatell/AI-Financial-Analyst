"""Renders one invoice to its delivered file: Factur-X PDF, UBL/CII XML, plain PDF or noisy scan."""

from __future__ import annotations

import io
import logging

import numpy as np
import pypdfium2 as pdfium
from facturx import generate_from_binary
from PIL import Image, ImageFilter

from ..models import Invoice
from .pdf import render_pdf
from .xml import build_cii, build_ubl

logging.getLogger("factur-x").setLevel(logging.ERROR)

EXTENSION = {"facturx": "pdf", "pdf": "pdf", "scan": "pdf", "ubl": "xml", "cii": "xml"}


def render_document(inv: Invoice, seed: int) -> bytes:
    if inv.format == "ubl":
        return build_ubl(inv)
    if inv.format == "cii":
        return build_cii(inv)
    pdf = render_pdf(inv)
    if inv.format == "facturx":
        return generate_from_binary(
            pdf, build_cii(inv), flavor="factur-x", level="en16931", check_xsd=False,
            pdf_metadata={"author": inv.seller_name, "keywords": "Factur-X, Invoice",
                          "title": f"{inv.seller_name}: {inv.number}", "subject": f"Invoice {inv.number}"},
        )
    if inv.format == "scan":
        return scan(pdf, np.random.default_rng([seed, int(inv.doc_id.split("-")[1])]))
    return pdf


def scan(pdf: bytes, rng: np.random.Generator) -> bytes:
    """Rasterise and degrade like a cheap office scanner: skew, blur, noise, speckles, JPEG."""
    page = pdfium.PdfDocument(pdf)[0]
    dpi = float(rng.uniform(110, 160))
    img = page.render(scale=dpi / 72).to_pil().convert("L")

    img = img.rotate(float(rng.uniform(-2.5, 2.5)), resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(float(rng.uniform(0.3, 1.0))))
    arr = np.asarray(img, dtype=np.float32)
    arr = arr * rng.uniform(0.85, 1.0) + rng.uniform(0, 25)  # uneven exposure
    arr += rng.normal(0, rng.uniform(4, 12), arr.shape)
    n_speck = int(rng.integers(50, 400))
    ys, xs = rng.integers(0, arr.shape[0], n_speck), rng.integers(0, arr.shape[1], n_speck)
    arr[ys, xs] = rng.uniform(0, 90, n_speck)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    jpeg = io.BytesIO()
    img.save(jpeg, "JPEG", quality=int(rng.integers(30, 65)))
    out = io.BytesIO()
    Image.open(jpeg).save(out, "PDF", resolution=dpi)
    return out.getvalue()
