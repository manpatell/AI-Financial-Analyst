"""Human-readable invoice PDFs in three layouts, French or English, with varied date/number formats."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from ..ids import format_iban
from ..models import Invoice

W, H = A4
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]
ACCENTS = [HexColor("#1f4e79"), HexColor("#7a2e2e"), HexColor("#2f5d3a")]

LABELS = {
    "fr": dict(invoice="FACTURE", credit_note="AVOIR", number="N° de facture", date="Date d'émission",
               due="Date d'échéance", buyer="Facturé à", desc="Désignation", qty="Qté", unit="PU HT",
               vat="TVA", amount="Montant HT", net="Total HT", vat_total="Total TVA", total="Total TTC",
               pay="Règlement par virement", ref="Facture d'origine", siren="SIREN", vatno="N° TVA"),
    "en": dict(invoice="INVOICE", credit_note="CREDIT NOTE", number="Invoice no.", date="Invoice date",
               due="Due date", buyer="Bill to", desc="Description", qty="Qty", unit="Unit price",
               vat="VAT", amount="Amount", net="Subtotal", vat_total="VAT", total="Total due",
               pay="Payment details", ref="Original invoice", siren="Company no.", vatno="VAT no."),
}


def fmt_date(d: date, template: int, lang: str) -> str:
    if lang == "en":
        return d.strftime("%b %d, %Y")
    return [d.strftime("%d/%m/%Y"), f"{d.day} {MOIS[d.month - 1]} {d.year}", d.isoformat()][template]


def fmt_money(x: Decimal, currency: str, lang: str, with_symbol: bool = True) -> str:
    if lang == "en":
        sym = {"USD": "$", "EUR": "€"}.get(currency, currency + " ")
        return f"{sym}{x:,.2f}" if with_symbol else f"{x:,.2f}"
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €" if with_symbol else s


def fmt_qty(q: Decimal) -> str:
    return f"{q:,.0f}".replace(",", " ") if q == q.to_integral() else f"{q}"


def fmt_rate(r: Decimal) -> str:
    return f"{r.normalize():f}".replace(".", ",") + " %"


def render_pdf(inv: Invoice) -> bytes:
    lang = "fr" if inv.seller_address.country == "FR" else "en"
    L = LABELS[lang]
    t = inv.template % 3
    accent = ACCENTS[t]
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4, pageCompression=1)
    c.setTitle(f"{L[inv.doc_type]} {inv.number}")
    c.setAuthor(inv.seller_name)

    def text(x, y, s, size=9, bold=False, color=black, align="left"):
        c.setFillColor(color)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        {"left": c.drawString, "right": c.drawRightString, "center": c.drawCentredString}[align](x, y, s)

    seller_lines = [inv.seller_address.street, f"{inv.seller_address.postcode} {inv.seller_address.city}"]
    if inv.seller_siren:
        seller_lines.append(f"{L['siren']} : {inv.seller_siren}")
    if inv.seller_vat:
        seller_lines.append(f"{L['vatno']} : {inv.seller_vat}")
    buyer_lines = [inv.buyer_name, inv.buyer_address.street,
                   f"{inv.buyer_address.postcode} {inv.buyer_address.city}"]
    if inv.buyer_vat:
        buyer_lines.append(f"{L['vatno']} : {inv.buyer_vat}")
    meta = [(L["number"], inv.number), (L["date"], fmt_date(inv.issue_date, t, lang)),
            (L["due"], fmt_date(inv.due_date, t, lang))]
    if inv.references_number:
        meta.append((L["ref"], inv.references_number))
    title = L[inv.doc_type]

    # ---- header
    if t == 0:
        text(40, H - 60, inv.seller_name, 14, True, accent)
        for i, s in enumerate(seller_lines):
            text(40, H - 78 - i * 12, s, 8.5)
        text(W - 40, H - 60, title, 20, True, accent, "right")
        y = H - 90
        for k, val in meta:
            text(W - 150, y, f"{k} :", 8.5)
            text(W - 40, y, val, 8.5, True, align="right")
            y -= 13
        c.setStrokeColor(accent)
        c.rect(W - 260, H - 260, 220, 80)
        for i, s in enumerate([L["buyer"]] + buyer_lines):
            text(W - 250, H - 195 - i * 12, s, 8.5, i == 0)
        table_top = H - 300
    elif t == 1:
        c.setFillColor(accent)
        c.rect(0, H - 110, W, 110, fill=1, stroke=0)
        text(40, H - 55, inv.seller_name.upper(), 16, True, white)
        text(40, H - 75, " · ".join(seller_lines[:2]), 8.5, color=white)
        text(40, H - 90, " · ".join(seller_lines[2:]), 8.5, color=white)
        text(W - 40, H - 60, title, 22, True, white, "right")
        text(W - 40, H - 80, inv.number, 11, color=white, align="right")
        for i, s in enumerate([L["buyer"]] + buyer_lines):
            text(40, H - 145 - i * 12, s, 9, i == 0)
        y = H - 145
        for k, val in meta:
            text(W - 200, y, k, 8.5)
            text(W - 40, y, val, 8.5, True, align="right")
            y -= 13
        table_top = H - 240
    else:
        text(W / 2, H - 55, inv.seller_name, 13, True, align="center")
        text(W / 2, H - 70, " - ".join(seller_lines), 7.5, align="center")
        text(40, H - 115, f"{title} {inv.number}", 15, True, accent)
        y = H - 140
        for k, val in meta[1:]:
            text(40, y, f"{k}: {val}", 9)
            y -= 13
        for i, s in enumerate([L["buyer"]] + buyer_lines):
            text(W - 220, H - 115 - i * 12, s, 8.5, i == 0)
        table_top = H - 215

    # ---- lines table
    cols = [40, 330, 385, 460, 500, W - 40]
    c.setFillColor(accent if t == 1 else HexColor("#e8e8e8"))
    c.rect(cols[0], table_top - 4, cols[-1] - cols[0], 16, fill=1, stroke=0)
    hdr_color = white if t == 1 else black
    for x, label, align in [(cols[0] + 4, L["desc"], "left"), (cols[2] - 4, L["qty"], "right"),
                            (cols[3] - 4, L["unit"], "right"), (cols[4] - 4, L["vat"], "right"),
                            (cols[5] - 4, L["amount"], "right")]:
        text(x, table_top, label, 8.5, True, hdr_color, align)
    y = table_top - 18
    for i, ln in enumerate(inv.lines):
        if t == 1 and i % 2:
            c.setFillColor(HexColor("#f3f3f3"))
            c.rect(cols[0], y - 4, cols[-1] - cols[0], 15, fill=1, stroke=0)
        text(cols[0] + 4, y, ln.description[:62], 8.5)
        text(cols[2] - 4, y, fmt_qty(ln.quantity), 8.5, align="right")
        text(cols[3] - 4, y, fmt_money(ln.unit_price, inv.currency, lang, t != 2), 8.5, align="right")
        text(cols[4] - 4, y, fmt_rate(ln.vat_rate), 8.5, align="right")
        text(cols[5] - 4, y, fmt_money(ln.amount, inv.currency, lang, t != 2), 8.5, align="right")
        if t == 0:
            c.setStrokeColor(HexColor("#cccccc"))
            c.line(cols[0], y - 5, cols[-1], y - 5)
        y -= 16

    # ---- totals
    y -= 14
    for label, val, bold in [(L["net"], inv.net, False), (L["vat_total"], inv.vat, False),
                             (L["total"], inv.total, True)]:
        text(W - 180, y, label, 9.5 if bold else 9, bold)
        text(W - 44, y, fmt_money(val, inv.currency, lang), 9.5 if bold else 9, bold, align="right")
        y -= 15
    if inv.note:
        text(40, y - 10, inv.note, 8)

    # ---- payment + footer
    text(40, 120, L["pay"], 9, True, accent)
    text(40, 106, f"IBAN : {format_iban(inv.iban)}", 9)
    text(40, 93, f"BIC : {inv.bic}", 9)
    if lang == "fr":
        text(40, 60, "Pénalités de retard : 3 fois le taux d'intérêt légal. Indemnité forfaitaire pour frais "
                     "de recouvrement : 40 €.", 7)
        text(40, 50, "Pas d'escompte pour paiement anticipé.", 7)
    c.showPage()
    c.save()
    return buf.getvalue()
