"""EN 16931 structured invoices: UN/CEFACT CII (also embedded in Factur-X) and OASIS UBL 2.1.

Element order follows the XSDs; tests validate output with the factur-x library's bundled schemas.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from lxml import etree

from ..models import Invoice, money

RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
UBL_INV = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CN = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
EN16931 = "urn:cen.eu:en16931:2017"


def _vat_category(inv: Invoice, rate: Decimal) -> str:
    if rate > 0:
        return "S"
    return "AE" if inv.seller_address.country != "FR" else "E"


def _breakdown(inv: Invoice) -> list[tuple[Decimal, Decimal, Decimal]]:
    """[(rate, basis, tax)] consistent with Invoice.vat (same rounding as invoices.totals)."""
    by_rate: dict[Decimal, Decimal] = defaultdict(Decimal)
    for ln in inv.lines:
        by_rate[ln.vat_rate] += ln.amount
    return [(r, b, money(b * r / 100)) for r, b in sorted(by_rate.items())]


def _amt(x: Decimal) -> str:
    return f"{x:.2f}"


def _rate(r: Decimal) -> str:
    return f"{r.normalize():f}"


def _qty(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _price(p: Decimal) -> str:
    return f"{p.normalize():f}" if p < 1 else f"{p:.2f}"


# ------------------------------------------------------------------- CII


def build_cii(inv: Invoice) -> bytes:
    nsmap = {"rsm": RSM, "ram": RAM, "udt": UDT, "qdt": QDT}

    def E(parent, ns, tag, text=None, **attrs):
        el = etree.SubElement(parent, f"{{{ns}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    def date_el(parent, tag, d):
        wrap = E(parent, RAM, tag)
        E(wrap, UDT, "DateTimeString", d.strftime("%Y%m%d"), format="102")

    root = etree.Element(f"{{{RSM}}}CrossIndustryInvoice", nsmap=nsmap)
    ctx = E(root, RSM, "ExchangedDocumentContext")
    E(E(ctx, RAM, "GuidelineSpecifiedDocumentContextParameter"), RAM, "ID", EN16931)

    doc = E(root, RSM, "ExchangedDocument")
    E(doc, RAM, "ID", inv.number)
    E(doc, RAM, "TypeCode", "381" if inv.doc_type == "credit_note" else "380")
    date_el(doc, "IssueDateTime", inv.issue_date)
    if inv.note:
        E(E(doc, RAM, "IncludedNote"), RAM, "Content", inv.note)

    tx = E(root, RSM, "SupplyChainTradeTransaction")
    for ln in inv.lines:
        item = E(tx, RAM, "IncludedSupplyChainTradeLineItem")
        E(E(item, RAM, "AssociatedDocumentLineDocument"), RAM, "LineID", str(ln.line_no))
        E(E(item, RAM, "SpecifiedTradeProduct"), RAM, "Name", ln.description)
        agr = E(item, RAM, "SpecifiedLineTradeAgreement")
        E(E(agr, RAM, "NetPriceProductTradePrice"), RAM, "ChargeAmount", _price(ln.unit_price))
        E(E(item, RAM, "SpecifiedLineTradeDelivery"), RAM, "BilledQuantity", _qty(ln.quantity),
          unitCode="C62")
        st = E(item, RAM, "SpecifiedLineTradeSettlement")
        tax = E(st, RAM, "ApplicableTradeTax")
        E(tax, RAM, "TypeCode", "VAT")
        E(tax, RAM, "CategoryCode", _vat_category(inv, ln.vat_rate))
        E(tax, RAM, "RateApplicablePercent", _rate(ln.vat_rate))
        E(E(st, RAM, "SpecifiedTradeSettlementLineMonetarySummation"), RAM, "LineTotalAmount",
          _amt(ln.amount))

    agr = E(tx, RAM, "ApplicableHeaderTradeAgreement")
    for role, name, siren, vat, addr in [
        ("SellerTradeParty", inv.seller_name, inv.seller_siren, inv.seller_vat, inv.seller_address),
        ("BuyerTradeParty", inv.buyer_name, inv.buyer_siren, inv.buyer_vat, inv.buyer_address),
    ]:
        p = E(agr, RAM, role)
        E(p, RAM, "Name", name)
        if siren:
            E(E(p, RAM, "SpecifiedLegalOrganization"), RAM, "ID", siren, schemeID="0002")
        a = E(p, RAM, "PostalTradeAddress")
        E(a, RAM, "PostcodeCode", addr.postcode)
        E(a, RAM, "LineOne", addr.street)
        E(a, RAM, "CityName", addr.city)
        E(a, RAM, "CountryID", addr.country)
        if vat:
            E(E(p, RAM, "SpecifiedTaxRegistration"), RAM, "ID", vat, schemeID="VA")

    E(tx, RAM, "ApplicableHeaderTradeDelivery")

    st = E(tx, RAM, "ApplicableHeaderTradeSettlement")
    E(st, RAM, "InvoiceCurrencyCode", inv.currency)
    pm = E(st, RAM, "SpecifiedTradeSettlementPaymentMeans")
    E(pm, RAM, "TypeCode", "58")
    E(E(pm, RAM, "PayeePartyCreditorFinancialAccount"), RAM, "IBANID", inv.iban)
    for rate, basis, tax_amt in _breakdown(inv):
        t = E(st, RAM, "ApplicableTradeTax")
        E(t, RAM, "CalculatedAmount", _amt(tax_amt))
        E(t, RAM, "TypeCode", "VAT")
        if rate == 0 and inv.seller_address.country != "FR":
            E(t, RAM, "ExemptionReason", "Reverse charge")
        E(t, RAM, "BasisAmount", _amt(basis))
        E(t, RAM, "CategoryCode", _vat_category(inv, rate))
        if rate == 0 and inv.seller_address.country == "FR":
            E(t, RAM, "ExemptionReasonCode", "VATEX-EU-132")
        E(t, RAM, "RateApplicablePercent", _rate(rate))
    terms = E(st, RAM, "SpecifiedTradePaymentTerms")
    date_el(terms, "DueDateDateTime", inv.due_date)
    tot = E(st, RAM, "SpecifiedTradeSettlementHeaderMonetarySummation")
    E(tot, RAM, "LineTotalAmount", _amt(inv.net))
    E(tot, RAM, "TaxBasisTotalAmount", _amt(inv.net))
    E(tot, RAM, "TaxTotalAmount", _amt(inv.vat), currencyID=inv.currency)
    E(tot, RAM, "GrandTotalAmount", _amt(inv.total))
    E(tot, RAM, "DuePayableAmount", _amt(inv.total))
    if inv.references_number:
        E(E(st, RAM, "InvoiceReferencedDocument"), RAM, "IssuerAssignedID", inv.references_number)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ------------------------------------------------------------------- UBL


def build_ubl(inv: Invoice) -> bytes:
    credit = inv.doc_type == "credit_note"
    root_ns = UBL_CN if credit else UBL_INV
    root = etree.Element(f"{{{root_ns}}}{'CreditNote' if credit else 'Invoice'}",
                         nsmap={None: root_ns, "cac": CAC, "cbc": CBC})
    cur = inv.currency

    def B(parent, tag, text=None, **attrs):
        el = etree.SubElement(parent, f"{{{CBC}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    def A(parent, tag):
        return etree.SubElement(parent, f"{{{CAC}}}{tag}")

    def tax_category(parent, tag, rate):
        tc = A(parent, tag)
        B(tc, "ID", _vat_category(inv, rate))
        B(tc, "Percent", _rate(rate))
        if rate == 0:
            B(tc, "TaxExemptionReason", "Reverse charge" if inv.seller_address.country != "FR"
              else "Exonération de TVA, article 261 C du CGI")
        B(A(tc, "TaxScheme"), "ID", "VAT")

    B(root, "CustomizationID", EN16931)
    B(root, "ID", inv.number)
    B(root, "IssueDate", inv.issue_date.isoformat())
    if credit:
        B(root, "CreditNoteTypeCode", "381")
    else:
        B(root, "DueDate", inv.due_date.isoformat())
        B(root, "InvoiceTypeCode", "380")
    if inv.note:
        B(root, "Note", inv.note)
    B(root, "DocumentCurrencyCode", cur)
    if inv.references_number:
        B(A(A(root, "BillingReference"), "InvoiceDocumentReference"), "ID", inv.references_number)

    for role, name, siren, vat, addr in [
        ("AccountingSupplierParty", inv.seller_name, inv.seller_siren, inv.seller_vat, inv.seller_address),
        ("AccountingCustomerParty", inv.buyer_name, inv.buyer_siren, inv.buyer_vat, inv.buyer_address),
    ]:
        party = A(A(root, role), "Party")
        B(A(party, "PartyName"), "Name", name)
        pa = A(party, "PostalAddress")
        B(pa, "StreetName", addr.street)
        B(pa, "CityName", addr.city)
        B(pa, "PostalZone", addr.postcode)
        B(A(pa, "Country"), "IdentificationCode", addr.country)
        if vat:
            pts = A(party, "PartyTaxScheme")
            B(pts, "CompanyID", vat)
            B(A(pts, "TaxScheme"), "ID", "VAT")
        ple = A(party, "PartyLegalEntity")
        B(ple, "RegistrationName", name)
        if siren:
            B(ple, "CompanyID", siren, schemeID="0002")

    pm = A(root, "PaymentMeans")
    B(pm, "PaymentMeansCode", "58")
    if credit:
        B(pm, "PaymentDueDate", inv.due_date.isoformat())
    B(A(pm, "PayeeFinancialAccount"), "ID", inv.iban)

    tt = A(root, "TaxTotal")
    B(tt, "TaxAmount", _amt(inv.vat), currencyID=cur)
    for rate, basis, tax_amt in _breakdown(inv):
        sub = A(tt, "TaxSubtotal")
        B(sub, "TaxableAmount", _amt(basis), currencyID=cur)
        B(sub, "TaxAmount", _amt(tax_amt), currencyID=cur)
        tax_category(sub, "TaxCategory", rate)

    lmt = A(root, "LegalMonetaryTotal")
    B(lmt, "LineExtensionAmount", _amt(inv.net), currencyID=cur)
    B(lmt, "TaxExclusiveAmount", _amt(inv.net), currencyID=cur)
    B(lmt, "TaxInclusiveAmount", _amt(inv.total), currencyID=cur)
    B(lmt, "PayableAmount", _amt(inv.total), currencyID=cur)

    for ln in inv.lines:
        line = A(root, "CreditNoteLine" if credit else "InvoiceLine")
        B(line, "ID", str(ln.line_no))
        B(line, "CreditedQuantity" if credit else "InvoicedQuantity", _qty(ln.quantity), unitCode="C62")
        B(line, "LineExtensionAmount", _amt(ln.amount), currencyID=cur)
        item = A(line, "Item")
        B(item, "Name", ln.description)
        tax_category(item, "ClassifiedTaxCategory", ln.vat_rate)
        B(A(line, "Price"), "PriceAmount", _price(ln.unit_price), currencyID=cur)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
