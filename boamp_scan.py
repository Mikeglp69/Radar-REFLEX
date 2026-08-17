```python
#!/usr/bin/env python3
"""
boamp_scan.py — Veille des appels d'offres WMS / Reflex
                 sur BOAMP + LinkedIn

Sources :
    - BOAMP : API officielle DILA
    - LinkedIn : résultats publics indexés via moteur de recherche

Variables d'environnement :

    SMTP_HOST
    SMTP_PORT
    SMTP_USER
    SMTP_PASSWORD
    ALERT_EMAIL_FROM
    ALERT_EMAIL_TO

    MIN_SCORE_FOR_ALERT       (défaut 3)

    SERPER_API_KEY             (optionnel)
                               Si absent, la recherche LinkedIn est ignorée.

    LINKEDIN_RESULTS_LIMIT     (défaut 20)
"""

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests


# ============================================================================
# CONFIGURATION
# ============================================================================

API_BASE = (
    "https://boamp-datadila.opendatasoft.com/api/explore/v2.1/"
    "catalog/datasets/boamp/records"
)

SERPER_API_URL = "https://google.serper.dev/search"

SEEN_IDS_FILE = Path(__file__).parent / "seen_ids.json"

MIN_SCORE_FOR_ALERT = int(
    os.environ.get("MIN_SCORE_FOR_ALERT", "3")
)

LINKEDIN_RESULTS_LIMIT = int(
    os.environ.get("LINKEDIN_RESULTS_LIMIT", "20")
)


# ============================================================================
# RECHERCHE
# ============================================================================

SEARCH_QUERY = (
    '(WMS OR REFLEX OR Hardis '
    'OR "Consultant fonctionnel WMS REFLEX" '
    'OR "Chef de projet WMS REFLEX" '
    'OR "gestion d\'entrepôt" '
    'OR "gestion d\'entrepôts" '
    'OR "système de gestion d\'entrepôt" '
    'OR "warehouse management")'
)


# Recherche LinkedIn volontairement plus ciblée.
# On recherche notamment les offres, missions, besoins et publications
# pouvant concerner Reflex / WMS.
LINKEDIN_SEARCH_QUERIES = [
    'site:linkedin.com/jobs/view '
    '(WMS OR REFLEX OR Hardis OR "warehouse management")',

    'site:linkedin.com/posts '
    '(WMS OR REFLEX OR Hardis OR "gestion d\'entrepôt")',

    'site:linkedin.com/feed/update '
    '(WMS OR REFLEX OR Hardis OR "gestion d\'entrepôt")',
]


# ============================================================================
# SCORING
# ============================================================================

HIGH_SIGNAL = [
    "reflex",
    "hardis",
]

MID_SIGNAL = [
    "REFLEX",
    "HARDIS",
    "WMS",
    "gestion d'entrepôt",
    "gestion d'entrepôts",
    "warehouse management",
    "système de gestion d'entrepôt",
    "consultant",
    "chef de projet",
]

LOW_SIGNAL = [
    "logistique",
    "entrepôt",
    "plateforme logistique",
    "système d'information logistique",
    "sge",
]


def score_text(text: str) -> int:
    """
    Calcule un score de pertinence de 0 à 5.
    """

    t = text.lower()

    # Signal très fort
    if any(k.lower() in t for k in HIGH_SIGNAL):
        return 5

    # Signaux moyens
    mid_hits = sum(
        1 for k in MID_SIGNAL
        if k.lower() in t
    )

    if mid_hits >= 2:
        return 4

    if mid_hits == 1:
        return 3

    # Signaux faibles
    low_hits = sum(
        1 for k in LOW_SIGNAL
        if k.lower() in t
    )

    if low_hits >= 2:
        return 2

    if low_hits == 1:
        return 1

    return 0


# ============================================================================
# BOAMP
# ============================================================================

def fetch_boamp_records() -> list[dict]:
    """
    Interroge l'API BOAMP.
    """

    params = {
        "q": SEARCH_QUERY,
        "limit": 1000000,
    }

    resp = requests.get(
        API_BASE,
        params=params,
        timeout=30,
    )

    resp.raise_for_status()

    data = resp.json()

    return data.get("results", [])


def parse_boamp_record(raw: dict) -> dict:
    """
    Transforme un enregistrement BOAMP en format commun.
    """

    fields = raw.get("record", {}).get("fields", raw)

    objet = (
        fields.get("objet")
        or fields.get("titre")
        or "Objet non renseigné"
    )

    acheteur = (
        fields.get("nomacheteur")
        or fields.get("nom_acheteur")
        or "Acheteur non précisé"
    )

    dept = (
        fields.get("code_departement")
        or fields.get("departement")
        or ""
    )

    date_parution = (
        fields.get("dateparution")
        or fields.get("date_parution")
        or ""
    )

    url_avis = (
        fields.get("url_avis")
        or fields.get("url")
        or ""
    )

    record_id = (
        raw.get("record", {}).get("id")
        or fields.get("idweb")
        or url_avis
        or objet
    )

    description = fields.get(
        "descripteur_libelle",
        ""
    )

    full_text = " ".join(
        [
            objet,
            acheteur,
            description,
        ]
    )

    return {
        "id": f"boamp:{record_id}",
        "source": "BOAMP",
        "objet": objet,
        "acheteur": acheteur,
        "dept": dept,
        "date_parution": date_parution,
        "url_avis": url_avis,
        "score": score_text(full_text),
    }


# ============================================================================
# LINKEDIN
# ============================================================================

def fetch_linkedin_results() -> list[dict]:
    """
    Recherche des résultats LinkedIn publics via Serper.

    Cela évite de scraper directement LinkedIn.
    """

    api_key = os.environ.get("SERPER_API_KEY")

    if not api_key:
        print(
            "→ SERPER_API_KEY absente : recherche LinkedIn ignorée."
        )
        return []

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    results = []

    for query in LINKEDIN_SEARCH_QUERIES:

        payload = {
            "q": query,
            "num": LINKEDIN_RESULTS_LIMIT,
            "gl": "fr",
            "hl": "fr",
        }

        try:
            resp = requests.post(
                SERPER_API_URL,
                headers=headers,
                json=payload,
                timeout=30,
            )

            resp.raise_for_status()

            data = resp.json()

        except requests.RequestException as exc:
            print(
                f"✗ Erreur recherche LinkedIn : {exc}",
                file=sys.stderr,
            )
            continue

        for item in data.get("organic", []):

            url = item.get("link", "")

            # Sécurité : on ne garde que LinkedIn.
            hostname = urlparse(url).netloc.lower()

            if "linkedin.com" not in hostname:
                continue

            title = item.get(
                "title",
                "Résultat LinkedIn",
            )

            snippet = item.get(
                "snippet",
                "",
            )

            results.append(
                {
                    "title": title,
                    "snippet": snippet,
                    "url": url,
                }
            )

    return results


def parse_linkedin_record(raw: dict) -> dict:
    """
    Transforme un résultat LinkedIn en format commun.
    """

    title = raw.get(
        "title",
        "Résultat LinkedIn",
    )

    snippet = raw.get(
        "snippet",
        "",
    )

    url = raw.get(
        "url",
        "",
    )

    full_text = " ".join(
        [
            title,
            snippet,
        ]
    )

    # L'URL constitue une partie de l'identifiant.
    record_id = f"linkedin:{url}"

    return {
        "id": record_id,
        "source": "LinkedIn",
        "objet": title,
        "acheteur": "",
        "dept": "",
        "date_parution": "",
        "url_avis": url,
        "score": score_text(full_text),
        "description": snippet,
    }


# ============================================================================
# DÉDUPLICATION
# ============================================================================

def deduplicate_records(records: list[dict]) -> list[dict]:
    """
    Supprime les doublons basés sur l'identifiant.
    """

    seen = set()
    output = []

    for record in records:

        record_id = record["id"]

        if record_id in seen:
            continue

        seen.add(record_id)
        output.append(record)

    return output


# ============================================================================
# HISTORIQUE
# ============================================================================

def load_seen_ids() -> set[str]:

    if SEEN_IDS_FILE.exists():

        try:
            return set(
                json.loads(
                    SEEN_IDS_FILE.read_text(
                        encoding="utf-8"
                    )
                )
            )

        except (json.JSONDecodeError, OSError):

            print(
                "⚠ Impossible de lire seen_ids.json, "
                "historique réinitialisé."
            )

    return set()


def save_seen_ids(ids: set[str]) -> None:

    SEEN_IDS_FILE.write_text(
        json.dumps(
            sorted(ids),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================================
# EMAIL
# ============================================================================

def build_email_body(
    new_matches: list[dict],
) -> str:

    lines = [
        f"Radar Reflex — {len(new_matches)} "
        f"nouvel(aux) résultat(s) détecté(s)",

        (
            "Généré le "
            f"{datetime.now(timezone.utc)"
            ".astimezone()"
            ".strftime('%d/%m/%Y à %H:%M')}"
        ),

        "",
    ]

    # Score décroissant puis source
    sorted_matches = sorted(
        new_matches,
        key=lambda x: (
            -x["score"],
            x["source"],
        ),
    )

    for match in sorted_matches:

        lines.append(
            f"[{match['score']}/5] "
            f"[{match['source']}] "
            f"{match['objet']}"
        )

        if match["source"] == "BOAMP":

            if match["acheteur"]:
                line = (
                    f"  Acheteur : "
                    f"{match['acheteur']}"
                )

                if match["dept"]:
                    line += (
                        f" (dépt. {match['dept']})"
                    )

                lines.append(line)

            if match["date_parution"]:
                lines.append(
                    f"  Publié le : "
                    f"{match['date_parution']}"
                )

        elif match["source"] == "LinkedIn":

            if match.get("description"):
                lines.append(
                    f"  {match['description']}"
                )

        if match["url_avis"]:
            lines.append(
                f"  Lien : {match['url_avis']}"
            )

        lines.append("")

    return "\n".join(lines)


def send_email(
    body: str,
    subject: str,
) -> None:

    host = os.environ["SMTP_HOST"]

    port = int(
        os.environ.get(
            "SMTP_PORT",
            "587",
        )
    )

    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]

    sender = os.environ.get(
        "ALERT_EMAIL_FROM",
        user,
    )

    recipients = [
        addr.strip()
        for addr in os.environ[
            "ALERT_EMAIL_TO"
        ].split(",")
    ]

    msg = MIMEMultipart()

    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    msg.attach(
        MIMEText(
            body,
            "plain",
            "utf-8",
        )
    )

    with smtplib.SMTP(
        host,
        port,
    ) as server:

        server.starttls()

        server.login(
            user,
            password,
        )

        server.sendmail(
            sender,
            recipients,
            msg.as_string(),
        )


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:

    print(
        "→ Interrogation de l'API BOAMP…"
    )

    try:

        raw_boamp = fetch_boamp_records()

    except requests.RequestException as exc:

        print(
            f"✗ Échec de l'appel BOAMP : {exc}",
            file=sys.stderr,
        )

        return 1

    boamp_records = [
        parse_boamp_record(record)
        for record in raw_boamp
    ]

    print(
        f"→ {len(boamp_records)} résultats BOAMP récupérés."
    )

    # ------------------------------------------------------------------------
    # LINKEDIN
    # ------------------------------------------------------------------------

    print(
        "→ Recherche LinkedIn…"
    )

    linkedin_raw = fetch_linkedin_results()

    linkedin_records = [
        parse_linkedin_record(record)
        for record in linkedin_raw
    ]

    print(
        f"→ {len(linkedin_records)} résultats LinkedIn récupérés."
    )

    # ------------------------------------------------------------------------
    # FUSION
    # ------------------------------------------------------------------------

    all_records = deduplicate_records(
        boamp_records + linkedin_records
    )

    relevant = [
        record
        for record in all_records
        if record["score"] >= MIN_SCORE_FOR_ALERT
    ]

    print(
        f"→ {len(relevant)} résultats pertinents "
        f"(seuil {MIN_SCORE_FOR_ALERT}/5) "
        f"sur {len(all_records)} résultats."
    )

    # ------------------------------------------------------------------------
    # HISTORIQUE
    # ------------------------------------------------------------------------

    seen_ids = load_seen_ids()

    new_matches = [
        record
        for record in relevant
        if record["id"] not in seen_ids
    ]

    if not new_matches:

        print(
            "→ Aucun nouveau résultat "
            "depuis le dernier scan."
        )

        save_seen_ids(
            seen_ids
            | {
                record["id"]
                for record in relevant
            }
        )

        return 0

    # ------------------------------------------------------------------------
    # EMAIL
    # ------------------------------------------------------------------------

    print(
        f"→ {len(new_matches)} nouvel(aux) "
        "résultat(s). Envoi de l'email…"
    )

    subject = (
        "[Radar Reflex] "
        f"{len(new_matches)} nouvel(aux) "
        "résultat(s) BOAMP / LinkedIn"
    )

    body = build_email_body(
        new_matches
    )

    try:

        send_email(
            body,
            subject,
        )

        print(
            "✓ Email envoyé."
        )

    except Exception as exc:

        print(
            f"✗ Échec de l'envoi email : {exc}",
            file=sys.stderr,
        )

        return 1

    # On n'enregistre les résultats comme vus
    # qu'après l'envoi réussi.

    save_seen_ids(
        seen_ids
        | {
            record["id"]
            for record in relevant
        }
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
