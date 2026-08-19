#!/usr/bin/env python3
"""
boamp_scan.py — Veille des appels d'offres WMS / Reflex
                 sur BOAMP + LinkedIn

Sources :
    - BOAMP : API officielle DILA (marchés publics)
    - LinkedIn : résultats publics indexés via moteur de recherche (Serper)

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

    BOAMP_LOOKBACK_DAYS        (défaut 7)
                               Ne remonte que les avis publiés dans les
                               N derniers jours (évite de rescanner tout
                               l'historique — 1,6M+ annonces — à chaque run).

    BOAMP_DEBUG                (défaut absent)
                               Si défini à "1", affiche le JSON brut du
                               premier enregistrement BOAMP reçu, pour
                               vérifier/ajuster les noms de champs si l'API
                               a changé de schéma.

NOTE IMPORTANTE SUR LES CHAMPS BOAMP :
    L'API BOAMP (opendatasoft v2.1) ne documente pas un schéma de champs
    plats stable dans le temps : une partie du contenu de l'annonce peut
    être exposée sous forme de JSON imbriqué dans un champ "donnees"
    plutôt que sous forme de champs plats "objet"/"nomacheteur"/etc.
    Ce script essaie d'abord les champs plats usuels, puis se rabat sur
    une recherche dans "donnees" si besoin. Lance une fois le script avec
    BOAMP_DEBUG=1 pour vérifier que les bons champs sont bien remplis et
    ajuste FIELD_CANDIDATES ci-dessous si nécessaire.
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
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

MIN_SCORE_FOR_ALERT = int(os.environ.get("MIN_SCORE_FOR_ALERT", "3"))

LINKEDIN_RESULTS_LIMIT = int(os.environ.get("LINKEDIN_RESULTS_LIMIT", "20"))

BOAMP_LOOKBACK_DAYS = int(os.environ.get("BOAMP_LOOKBACK_DAYS", "7"))

BOAMP_DEBUG = os.environ.get("BOAMP_DEBUG") == "1"

# L'API opendatasoft v2.1 plafonne "limit" à 100 par requête : on pagine.
BOAMP_PAGE_SIZE = 100
BOAMP_MAX_PAGES = 20  # garde-fou : 2000 avis max par run


# ============================================================================
# RECHERCHE
# ============================================================================

SEARCH_QUERY = (
    '(Hardis WMS OR Reflex/WMS OR Hardis '
    'OR "Consultant fonctionnel Hardis WMS OR Reflex/WMS" '
    'OR "Chef de projet Reflex/WMS" '
    'OR "gestion d\'entrepôt" '
    'OR "gestion d\'entrepôts" '
    'OR "système de gestion d\'entrepôt" '
    'OR "warehouse management")'
)

# Recherche LinkedIn volontairement plus ciblée.
LINKEDIN_SEARCH_QUERIES = [
    'site:linkedin.com/jobs/view '
    '(Hardis WMS OR Reflex/WMS OR Hardis OR "warehouse management")',

    'site:linkedin.com/posts '
    '(Hardis WMS OR Reflex/WMS OR Hardis OR "gestion d\'entrepôt")',

    'site:linkedin.com/feed/update '
    '(Hardis WMS OR Reflex/WMS OR Hardis OR "gestion d\'entrepôt")',
]


# ============================================================================
# SCORING
# ============================================================================

# Signaux "mots courts" : on impose des frontières de mot (\b) pour éviter
# les faux positifs par sous-chaîne (ex: "WMS" ne doit pas matcher à
# l'intérieur d'un autre mot).
HIGH_SIGNAL_WORDS = ["Reflex/WMS", "Hardis WMS"]

MID_SIGNAL_WORDS = ["wms"]
MID_SIGNAL_PHRASES = [
    "gestion d'entrepôt",
    "gestion d'entrepôts",
    "warehouse management",
    "système de gestion d'entrepôt",
    "consultant",
    "chef de projet",
]

LOW_SIGNAL_PHRASES = [
    "logistique",
    "entrepôt",
    "plateforme logistique",
    "système d'information logistique",
]


def _word_hit(word: str, text_lower: str) -> bool:
    return re.search(r"\b" + re.escape(word) + r"\b", text_lower) is not None


def score_text(text: str) -> int:
    """
    Calcule un score de pertinence de 0 à 5.
    """
    t = (text or "").lower()

    # Signal très fort (Reflex / Hardis explicitement cités)
    if any(_word_hit(w, t) for w in HIGH_SIGNAL_WORDS):
        return 5

    # Signaux moyens
    mid_hits = sum(1 for w in MID_SIGNAL_WORDS if _word_hit(w, t))
    mid_hits += sum(1 for p in MID_SIGNAL_PHRASES if p in t)

    if mid_hits >= 2:
        return 4
    if mid_hits == 1:
        return 3

    # Signaux faibles
    low_hits = sum(1 for p in LOW_SIGNAL_PHRASES if p in t)

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
    Interroge l'API BOAMP avec pagination et une fenêtre de fraîcheur
    (BOAMP_LOOKBACK_DAYS) pour éviter de rescanner tout l'historique.
    """

    since = (
        datetime.now(timezone.utc) - timedelta(days=BOAMP_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")

    where_clause = f'dateparution >= "{since}"'

    all_results: list[dict] = []
    offset = 0

    for page in range(BOAMP_MAX_PAGES):
        params = {
            "q": SEARCH_QUERY,
            "where": where_clause,
            "limit": BOAMP_PAGE_SIZE,
            "offset": offset,
        }

        resp = requests.get(API_BASE, params=params, timeout=30)

        if page == 0:
            # Diagnostic systématique (pas besoin de BOAMP_DEBUG pour ça) :
            # on affiche toujours l'URL exacte interrogée et le nombre total
            # de résultats annoncés par l'API, pour distinguer "l'API ne
            # trouve rien" de "le filtrage local supprime tout".
            print(f"→ Requête BOAMP : {resp.url}")

        resp.raise_for_status()
        data = resp.json()

        if page == 0:
            print(f"→ total_count annoncé par l'API : {data.get('total_count')}")

        results = data.get("results", [])

        if BOAMP_DEBUG and page == 0 and results:
            print("=== BOAMP_DEBUG : premier enregistrement brut ===")
            print(json.dumps(results[0], ensure_ascii=False, indent=2))
            print("=== fin BOAMP_DEBUG ===")

        if page == 0 and not results:
            print(
                "⚠ 0 résultat renvoyé par l'API pour cette requête/filtre. "
                "Vérifie l'URL affichée ci-dessus (teste-la telle quelle dans "
                "un navigateur ou avec curl) pour voir si le problème vient "
                "du paramètre 'q' (ODSQL) ou du 'where' (champ dateparution)."
            )

        all_results.extend(results)

        if len(results) < BOAMP_PAGE_SIZE:
            break

        offset += BOAMP_PAGE_SIZE

    return all_results


def _flatten_to_text(value) -> str:
    """
    Certains champs BOAMP (ex: descripteur_libelle) peuvent être une liste
    plutôt qu'une chaîne. On aplati proprement pour éviter un crash.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_to_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flatten_to_text(v) for v in value.values())
    return str(value)


def _first_nonempty(fields: dict, *keys: str) -> str:
    for key in keys:
        value = fields.get(key)
        text = _flatten_to_text(value).strip()
        if text:
            return text
    return ""


def parse_boamp_record(raw: dict) -> dict | None:
    """
    Transforme un enregistrement BOAMP en format commun.
    Retourne None si l'enregistrement est inexploitable (au lieu de
    planter tout le run sur une seule ligne malformée).
    """
    try:
        # v2.1 renvoie les champs à plat directement dans l'objet ;
        # on garde le fallback v1 ("record"/"fields") par sécurité.
        fields = raw.get("record", {}).get("fields", raw) if isinstance(
            raw.get("record"), dict
        ) else raw

        objet = _first_nonempty(fields, "objet", "titre") or "Objet non renseigné"
        acheteur = _first_nonempty(
            fields, "nomacheteur", "nom_acheteur", "acheteur"
        ) or "Acheteur non précisé"
        dept = _first_nonempty(fields, "code_departement", "departement")
        date_parution = _first_nonempty(fields, "dateparution", "date_parution")
        url_avis = _first_nonempty(fields, "url_avis", "url")
        description = _first_nonempty(fields, "descripteur_libelle", "objet_complet")

        record_id = (
            raw.get("record", {}).get("id")
            if isinstance(raw.get("record"), dict)
            else None
        ) or _first_nonempty(fields, "idweb", "id") or url_avis or objet

        full_text = " ".join([objet, acheteur, description])

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
    except Exception as exc:
        print(f"⚠ Enregistrement BOAMP ignoré (erreur de parsing) : {exc}", file=sys.stderr)
        return None


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
        print("→ SERPER_API_KEY absente : recherche LinkedIn ignorée.")
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
                SERPER_API_URL, headers=headers, json=payload, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"✗ Erreur recherche LinkedIn : {exc}", file=sys.stderr)
            continue

        organic = data.get("organic", [])
        print(f"  · requête « {query} » → {len(organic)} résultat(s) bruts Serper")

        for item in organic:
            url = item.get("link", "")
            hostname = urlparse(url).netloc.lower()

            if "linkedin.com" not in hostname:
                continue

            results.append(
                {
                    "title": item.get("title", "Résultat LinkedIn"),
                    "snippet": item.get("snippet", ""),
                    "url": url,
                }
            )

        # Petite pause pour rester sous les seuils de rate-limit de Serper.
        time.sleep(0.5)

    return results


def parse_linkedin_record(raw: dict) -> dict:
    """
    Transforme un résultat LinkedIn en format commun.
    """
    title = raw.get("title", "Résultat LinkedIn")
    snippet = raw.get("snippet", "")
    url = raw.get("url", "")

    full_text = " ".join([title, snippet])
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
            return set(json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            print("⚠ Impossible de lire seen_ids.json, historique réinitialisé.")
    return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_IDS_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================================
# EMAIL
# ============================================================================

def build_email_body(new_matches: list[dict]) -> str:
    generated_at = datetime.now(timezone.utc).astimezone().strftime(
        "%d/%m/%Y à %H:%M"
    )

    lines = [
        f"Radar Reflex — {len(new_matches)} nouvel(aux) résultat(s) détecté(s)",
        f"Généré le {generated_at}",
        "",
    ]

    sorted_matches = sorted(
        new_matches, key=lambda x: (-x["score"], x["source"])
    )

    for match in sorted_matches:
        lines.append(
            f"[{match['score']}/5] [{match['source']}] {match['objet']}"
        )

        if match["source"] == "BOAMP":
            if match["acheteur"]:
                line = f"  Acheteur : {match['acheteur']}"
                if match["dept"]:
                    line += f" (dépt. {match['dept']})"
                lines.append(line)

            if match["date_parution"]:
                lines.append(f"  Publié le : {match['date_parution']}")

        elif match["source"] == "LinkedIn":
            if match.get("description"):
                lines.append(f"  {match['description']}")

        if match["url_avis"]:
            lines.append(f"  Lien : {match['url_avis']}")

        lines.append("")

    return "\n".join(lines)


def send_email(body: str, subject: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("ALERT_EMAIL_FROM", user)

    recipients = [
        addr.strip() for addr in os.environ["ALERT_EMAIL_TO"].split(",")
    ]

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    print("→ Interrogation de l'API BOAMP…")

    try:
        raw_boamp = fetch_boamp_records()
    except requests.RequestException as exc:
        print(f"✗ Échec de l'appel BOAMP : {exc}", file=sys.stderr)
        return 1

    boamp_records = [
        r for r in (parse_boamp_record(rec) for rec in raw_boamp) if r is not None
    ]

    print(f"→ {len(boamp_records)} résultats BOAMP récupérés.")

    print("→ Recherche LinkedIn…")
    linkedin_raw = fetch_linkedin_results()
    linkedin_records = [parse_linkedin_record(r) for r in linkedin_raw]
    print(f"→ {len(linkedin_records)} résultats LinkedIn récupérés.")

    all_records = deduplicate_records(boamp_records + linkedin_records)

    relevant = [r for r in all_records if r["score"] >= MIN_SCORE_FOR_ALERT]

    print(
        f"→ {len(relevant)} résultats pertinents (seuil {MIN_SCORE_FOR_ALERT}/5) "
        f"sur {len(all_records)} résultats."
    )

    seen_ids = load_seen_ids()
    new_matches = [r for r in relevant if r["id"] not in seen_ids]

    if not new_matches:
        print("→ Aucun nouveau résultat depuis le dernier scan.")
        save_seen_ids(seen_ids | {r["id"] for r in relevant})
        return 0

    print(f"→ {len(new_matches)} nouvel(aux) résultat(s). Envoi de l'email…")

    subject = (
        f"[Radar Reflex] {len(new_matches)} nouvel(aux) résultat(s) BOAMP / LinkedIn"
    )
    body = build_email_body(new_matches)

    try:
        send_email(body, subject)
        print("✓ Email envoyé.")
    except Exception as exc:
        print(f"✗ Échec de l'envoi email : {exc}", file=sys.stderr)
        return 1

    # On n'enregistre les résultats comme vus qu'après l'envoi réussi.
    save_seen_ids(seen_ids | {r["id"] for r in relevant})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
