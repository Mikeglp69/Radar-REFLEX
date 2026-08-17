#!/usr/bin/env python3
"""
boamp_scan.py — Veille des appels d'offres WMS / Reflex sur le BOAMP

Interroge l'API officielle BOAMP (DILA), filtre et score les avis pertinents
pour le conseil en WMS spécialisé Reflex (éditeur Hardis), et envoie un digest
email pour les nouveaux avis détectés depuis la dernière exécution.

Conçu pour tourner sans serveur dédié via une tâche planifiée (voir README.md
pour la mise en place avec GitHub Actions, gratuite et suffisante à ce volume).

Variables d'environnement attendues :
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD  — serveur d'envoi
    ALERT_EMAIL_FROM, ALERT_EMAIL_TO                — expéditeur / destinataire(s)
    MIN_SCORE_FOR_ALERT (optionnel, défaut 3)       — seuil de pertinence 1-5
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

API_BASE = "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/boamp/records"
SEEN_IDS_FILE = Path(__file__).parent / "seen_ids.json"

# Même logique de recherche et de score que le site, à garder synchronisée avec index.html.
SEARCH_QUERY = (
    '(WMS OR Reflex OR Hardis OR "gestion d\'entrepôt" OR "gestion d\'entrepôts" '
    'OR "système de gestion d\'entrepôt" OR "warehouse management")'
)

HIGH_SIGNAL = ["reflex", "hardis"]
MID_SIGNAL = [
    "wms",
    "gestion d'entrepôt",
    "gestion d'entrepôts",
    "warehouse management",
    "système de gestion d'entrepôt",
]
LOW_SIGNAL = [
    "logistique",
    "entrepôt",
    "plateforme logistique",
    "système d'information logistique",
    "sge",
]

MIN_SCORE_FOR_ALERT = int(os.environ.get("MIN_SCORE_FOR_ALERT", "3"))


def score_text(text: str) -> int:
    t = text.lower()
    if any(k in t for k in HIGH_SIGNAL):
        return 5
    mid_hits = sum(1 for k in MID_SIGNAL if k in t)
    if mid_hits >= 2:
        return 4
    if mid_hits == 1:
        return 3
    low_hits = sum(1 for k in LOW_SIGNAL if k in t)
    if low_hits >= 2:
        return 2
    if low_hits == 1:
        return 1
    return 0


def fetch_records() -> list[dict]:
    """Interroge l'API BOAMP. NOTE : les noms de champs ci-dessous (objet,
    nomacheteur, code_departement, dateparution, url_avis, id) sont ceux
    couramment observés sur ce jeu de données ; vérifiez-les sur
    https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/information/
    avant la première mise en production, le schéma peut évoluer."""
    params = {
        "q": SEARCH_QUERY,
        "limit": 100,
    }
    resp = requests.get(API_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", [])


def parse_record(raw: dict) -> dict:
    fields = raw.get("record", {}).get("fields", raw)
    objet = fields.get("objet") or fields.get("titre") or "Objet non renseigné"
    acheteur = fields.get("nomacheteur") or fields.get("nom_acheteur") or "Acheteur non précisé"
    dept = fields.get("code_departement") or fields.get("departement") or ""
    date_parution = fields.get("dateparution") or fields.get("date_parution") or ""
    url_avis = fields.get("url_avis") or fields.get("url") or ""
    record_id = raw.get("record", {}).get("id") or fields.get("idweb") or url_avis or objet

    full_text = " ".join([objet, acheteur, fields.get("descripteur_libelle", "")])
    return {
        "id": str(record_id),
        "objet": objet,
        "acheteur": acheteur,
        "dept": dept,
        "date_parution": date_parution,
        "url_avis": url_avis,
        "score": score_text(full_text),
    }


def load_seen_ids() -> set[str]:
    if SEEN_IDS_FILE.exists():
        return set(json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_IDS_FILE.write_text(json.dumps(sorted(ids)), encoding="utf-8")


def build_email_body(new_matches: list[dict]) -> str:
    lines = [
        f"Radar Reflex — {len(new_matches)} nouvel(aux) avis détecté(s)",
        f"Généré le {datetime.now(timezone.utc).astimezone().strftime('%d/%m/%Y à %H:%M')}",
        "",
    ]
    for m in sorted(new_matches, key=lambda x: -x["score"]):
        lines.append(f"[{m['score']}/5] {m['objet']}")
        lines.append(f"  Acheteur : {m['acheteur']}" + (f" (dépt. {m['dept']})" if m["dept"] else ""))
        if m["date_parution"]:
            lines.append(f"  Publié le : {m['date_parution']}")
        if m["url_avis"]:
            lines.append(f"  Lien : {m['url_avis']}")
        lines.append("")
    return "\n".join(lines)


def send_email(body: str, subject: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("ALERT_EMAIL_FROM", user)
    recipients = [addr.strip() for addr in os.environ["ALERT_EMAIL_TO"].split(",")]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())


def main() -> int:
    print("→ Interrogation de l'API BOAMP…")
    try:
        raw_records = fetch_records()
    except requests.RequestException as exc:
        print(f"✗ Échec de l'appel API : {exc}", file=sys.stderr)
        return 1

    parsed = [parse_record(r) for r in raw_records]
    relevant = [r for r in parsed if r["score"] >= MIN_SCORE_FOR_ALERT]
    print(f"→ {len(relevant)} avis pertinents (seuil {MIN_SCORE_FOR_ALERT}/5) sur {len(parsed)} récupérés.")

    seen_ids = load_seen_ids()
    new_matches = [r for r in relevant if r["id"] not in seen_ids]

    if not new_matches:
        print("→ Aucun nouvel avis depuis le dernier scan. Pas d'email envoyé.")
        save_seen_ids(seen_ids | {r["id"] for r in relevant})
        return 0

    print(f"→ {len(new_matches)} nouvel(aux) avis. Envoi de l'email…")
    subject = f"[Radar Reflex] {len(new_matches)} nouvel(aux) appel(s) d'offres WMS détecté(s)"
    body = build_email_body(new_matches)

    try:
        send_email(body, subject)
        print("✓ Email envoyé.")
    except Exception as exc:
        print(f"✗ Échec de l'envoi email : {exc}", file=sys.stderr)
        return 1

    save_seen_ids(seen_ids | {r["id"] for r in relevant})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
