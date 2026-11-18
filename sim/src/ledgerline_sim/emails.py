"""Inbox of the accounts-payable mailbox: planted fraud / legit bank change emails plus noise."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from zoneinfo import ZoneInfo

import numpy as np

from .company import slug
from .config import SimConfig
from .ids import format_iban
from .models import Company, Email, Invoice, Vendor

PARIS = ZoneInfo("Europe/Paris")
CEO_NAME = "Bertrand Calvet"
AP_MAILBOX = "comptabilite@ateliers-calvet.fr"


def _at(d: date, rng: np.random.Generator) -> datetime:
    return datetime.combine(d, time(int(rng.integers(8, 19)), int(rng.integers(60))), tzinfo=PARIS)


def _eur(x) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " €"


def build_emails(cfg: SimConfig, rng: np.random.Generator, company: Company, vendors: dict[str, Vendor],
                 planned: list[dict], ap: list[Invoice]) -> list[Email]:
    out: list[Email] = []
    n = 0

    def add(**kw) -> None:
        nonlocal n
        n += 1
        out.append(Email(email_id=f"tmp-EML-{n}", to_addr=AP_MAILBOX, **kw))

    fraud_invoice = {t.split(":")[1]: inv for inv in ap for t in inv.tags if t.startswith("fraud:")}

    for p in planned:
        v: Vendor = p["vendor"]
        pid = p["plant_id"]
        if p["kind"] == "legit_bank_change":
            add(sent_at=_at(p["date"], rng), from_name=f"Service comptabilité {v.name}",
                from_addr=f"compta@{v.email_domain}", subject="Changement de coordonnées bancaires",
                body=(f"Bonjour,\n\nSuite au rapprochement de notre société avec un nouveau partenaire "
                      f"bancaire, nos coordonnées bancaires changent à compter du "
                      f"{p['date'] + timedelta(days=7):%d/%m/%Y}.\n\nNouvel IBAN : {format_iban(p['new_iban'])}\n\n"
                      f"Vous trouverez ci-joint notre nouveau RIB signé par notre gérant. N'hésitez pas à "
                      f"nous appeler au numéro habituel ({v.phone}) pour confirmation.\n\n"
                      f"Cordialement,\nService comptabilité\n{v.name}"),
                related_vendor_id=v.id, tags=[f"bankchange:{pid}:email"])
        elif p["kind"] == "fraud_bank_change":
            fake_phone = f"+33 6 {int(rng.integers(10, 99))} {int(rng.integers(10, 99))} " \
                         f"{int(rng.integers(10, 99))} {int(rng.integers(10, 99))}"
            add(sent_at=_at(p["date"], rng), from_name=f"Comptabilité {v.name}",
                from_addr=f"compta@{p['from_domain']}",
                reply_to=f"{slug(v.name)}.compta@mail-securise-pro.net",
                subject="URGENT - Mise à jour de nos coordonnées bancaires",
                body=(f"Bonjour,\n\nSuite à un audit interne, notre compte bancaire habituel est "
                      f"temporairement bloqué. Merci d'effectuer dès aujourd'hui tous vos règlements sur "
                      f"notre nouveau compte :\n\nIBAN : {format_iban(p['new_iban'])}\n\n"
                      f"Merci de ne pas utiliser l'ancien compte, les paiements seraient rejetés. "
                      f"Pour toute question, contactez directement notre responsable au {fake_phone}.\n\n"
                      f"Nous vous remercions de votre réactivité.\n\nService comptabilité\n{v.name}"),
                spf="softfail", dkim="none", related_vendor_id=v.id, tags=[f"fraud:{pid}:email"])
        elif p["kind"] == "fraud_ceo":
            inv = fraud_invoice[pid]
            add(sent_at=_at(p["date"], rng), from_name=CEO_NAME, from_addr=f"direction@{p['from_domain']}",
                reply_to="b.calvet.direction@mail-securise-pro.net",
                subject="Confidentiel - règlement urgent",
                body=(f"Bonjour,\n\nJe suis en déplacement et difficilement joignable aujourd'hui. Nous "
                      f"finalisons une opération confidentielle avec le cabinet {v.name}. Merci de régler "
                      f"en priorité leur facture {inv.number} de {_eur(inv.total)} avant la fin de semaine, "
                      f"sans en parler pour l'instant au reste de l'équipe.\n\nJe compte sur toi.\n\n"
                      f"{CEO_NAME}\nPrésident\n{company.name}\n\nEnvoyé depuis mon mobile"),
                spf="fail", dkim="none", related_vendor_id=v.id, tags=[f"fraud:{pid}:email"])

    # Noise: payment reminders, portal notifications, newsletters, internal mail.
    eur_transfer = [i for i in ap if i.doc_type == "invoice" and not i.tags
                    and vendors[i.vendor_id].pay_method == "transfer"]
    for i in rng.choice(len(eur_transfer), size=14, replace=False):
        inv = eur_transfer[i]
        d = inv.due_date + timedelta(days=int(rng.integers(3, 12)))
        if d > cfg.end:
            continue
        v = vendors[inv.vendor_id]
        add(sent_at=_at(d, rng), from_name=f"Recouvrement {v.name}", from_addr=f"compta@{v.email_domain}",
            subject=f"Relance - facture {inv.number} échue",
            body=(f"Bonjour,\n\nSauf erreur de notre part, la facture {inv.number} du "
                  f"{inv.issue_date:%d/%m/%Y} d'un montant de {_eur(inv.total)} reste impayée à ce jour. "
                  f"Si votre règlement a été effectué entre-temps, merci de ne pas tenir compte de ce "
                  f"message.\n\nCordialement,\n{v.name}"),
            related_vendor_id=v.id)
    portal = [i for i in ap if vendors[i.vendor_id].category == "saas" and i.doc_type == "invoice"]
    for i in rng.choice(len(portal), size=min(20, len(portal)), replace=False):
        inv = portal[i]
        v = vendors[inv.vendor_id]
        english = v.address.country != "FR"
        add(sent_at=_at(inv.issue_date, rng), from_name=v.name, from_addr=f"billing@{v.email_domain}",
            subject=(f"Your invoice {inv.number} is available" if english
                     else f"Votre facture {inv.number} est disponible"),
            body=(f"Hello,\n\nYour invoice {inv.number} for {inv.total} {inv.currency} is available in your "
                  f"account. It will be charged to your card on file.\n\nThanks,\nThe {v.name} team"
                  if english else
                  f"Bonjour,\n\nVotre facture {inv.number} d'un montant de {_eur(inv.total)} est disponible "
                  f"dans votre espace client.\n\nL'équipe {v.name}"),
            related_vendor_id=v.id)
    vlist = [v for v in vendors.values() if v.category in ("materials", "packaging", "marketing")]
    for _ in range(8):
        v = vlist[int(rng.integers(len(vlist)))]
        d = cfg.start + timedelta(days=int(rng.integers((cfg.end - cfg.start).days)))
        add(sent_at=_at(d, rng), from_name=v.name, from_addr=f"newsletter@{v.email_domain}",
            subject=f"{v.name} : nos nouveautés et tarifs {d.year}",
            body="Découvrez nos nouvelles références et nos conditions pour la saison à venir.",
            related_vendor_id=v.id)
    for subject, body in [
        ("Point trésorerie mensuel", "On fait le point jeudi sur les encaissements en retard ?"),
        ("Commande Arcadia", "Le bon de commande Arcadia est validé, facturation à la livraison."),
        ("Note de frais salon", "Je t'envoie les justificatifs du salon dans la journée."),
        ("Clôture mensuelle", "Peux-tu me transmettre les relevés bancaires du mois ?"),
    ]:
        d = cfg.start + timedelta(days=int(rng.integers((cfg.end - cfg.start).days)))
        add(sent_at=_at(d, rng), from_name="Claire Moreau", from_addr=f"c.moreau@{company.email_domain}",
            subject=subject, body=body)
    return out


def to_eml(e: Email) -> bytes:
    msg = EmailMessage()
    msg["From"] = f'"{e.from_name}" <{e.from_addr}>'
    msg["To"] = e.to_addr
    if e.reply_to:
        msg["Reply-To"] = e.reply_to
    msg["Subject"] = e.subject
    msg["Date"] = format_datetime(e.sent_at)
    msg["Message-ID"] = make_msgid(domain=e.from_addr.split("@")[1])
    msg["Authentication-Results"] = (f"mx.ateliers-calvet.fr; spf={e.spf} "
                                     f"smtp.mailfrom={e.from_addr.split('@')[1]}; dkim={e.dkim}")
    msg.set_content(e.body)
    return bytes(msg)
