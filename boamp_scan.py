#!/usr/bin/env python3
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
MARKET_RESULTS_LIMIT = int(os.environ.get("MARKET_RESULTS_LIMIT", "20"))
BOAMP_LOOKBACK_DAYS = int(os.environ.get("BOAMP_LOOKBACK_DAYS", "7"))
BOAMP_DEBUG = os.environ.get("BOAMP_DEBUG") == "1"

BOAMP_PAGE_SIZE = 100
BOAMP_MAX_PAGES = 20


SEARCH_QUERY = (
    '("Hardis WMS" OR "Hardis WMS (ex Reflex)" OR "Reflex WMS" '
    'OR "Reflex/WMS" OR "WMS Reflex" OR "Consultant Reflex" '
    'OR "Expert Reflex" OR "Chef de projet Reflex" '
    'OR "Consultant WMS" OR "Chef de projet WMS" '
    'OR "système de gestion d\'entrepôt" OR "warehouse management")'
)

MARKET_SEARCH_QUERIES = [
    '"Hardis WMS" France',
    '"Hardis WMS" freelance France',
    '"Hardis WMS" emploi France',
    '"Hardis WMS" "ex Reflex" France',
    '"Reflex WMS" France',
    '"Reflex WMS" freelance France',
    '"Reflex WMS" mission France',
    'site:indeed.com "Reflex WMS"',
    'site:linkedin.com/jobs "Reflex WMS"',
    'site:linkedin.com/jobs "Hardis WMS"',
    'site:hardis-group.com "Consultant WMS"',
    'site:free-work.com "Reflex WMS"',
    'site:freelance-informatique.fr Reflex WMS',
    'site:collective.work "Reflex WMS"',
    'site:katchme.fr "Reflex WMS"',
    'site:mindquest.io "Reflex WMS"',
]

LINKEDIN_SEARCH_QUERIES = [
    'site:linkedin.com/jobs/view ("Reflex WMS" OR "Hardis WMS" OR "Consultant Reflex")',
    'site:linkedin.com/posts ("Reflex WMS" OR "Hardis WMS" OR "Consultant Reflex")',
]


# ============================================================================
# SCORING / CLASSIFICATION
# ============================================================================

HIGH_SIGNAL_PHRASES = [
    "hardis wms",
    "hardis wms (ex reflex)",
    "reflex wms",
    "reflex/wms",
    "wms reflex",
    "consultant reflex",
    "expert reflex",
    "chef de projet reflex",
]

ROLE_PHRASES = [
    "consultant",
    "chef de projet",
    "expert",
    "responsable d'application",
    "amoa",
    "support",
    "paramétrage",
    "integration",
    "intégration",
    "déploiement",
    "migration",
]

MISSION_PHRASES = [
    "freelance",
    "mission",
    "indépendant",
    "independant",
    "régie",
    "regie",
    "tjm",
    "jours",
    "durée",
    "duree",
    "asap",
]

EMPLOYMENT_PHRASES = [
    "cdi",
    "cdd",
    "recrutement",
    "poste",
]

LOW_SIGNAL_PHRASES = [
    "logistique",
    "entrepôt",
    "warehouse",
    "supply chain",
    "wms",
]


def _norm(text: str) -> str:
    text = (text or "").lower()
    replacements = {
        "’": "'",
        "–": "-",
        "—": "-",
        "\xa0": " ",
    }
    for a, b in replacements.items():
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(text_lower: str, phrase: str) -> bool:
    return _norm(phrase) in text_lower


def classify_result(title: str, snippet: str, url: str = "") -> tuple[int, str, str]:
    t = _norm(" ".join([title, snippet, url]))

    high = sum(1 for p in HIGH_SIGNAL_PHRASES if _contains_phrase(t, p))
    roles = sum(1 for p in ROLE_PHRASES if _contains_phrase(t, p))
    mission = sum(1 for p in MISSION_PHRASES if _contains_phrase(t, p))
    employment = sum(1 for p in EMPLOYMENT_PHRASES if _contains_phrase(t, p))
    low = sum(1 for p in LOW_SIGNAL_PHRASES if _contains_phrase(t, p))

    if high >= 1 and mission >= 1:
        return 5, "MISSION_FREELANCE", "Reflex/Hardis WMS + signaux de mission"
    if high >= 1 and roles >= 1 and mission >= 1:
        return 5, "MISSION_FREELANCE", "Expertise Reflex/Hardis WMS et mission"
    if high >= 1 and roles >= 1:
        return 4, "BESOIN_PROJET", "Besoin opérationnel lié à Reflex/Hardis WMS"
    if high >= 1 and employment >= 1:
        return 3, "RECRUTEMENT", "Recrutement autour de Reflex/Hardis WMS"
    if high >= 1:
        return 3, "INFORMATION", "Mention directe de Reflex/Hardis WMS"
    if low >= 2 and roles >= 1:
        return 2, "INFORMATION", "WMS/logistique avec rôle fonctionnel"
    return 0, "HORS_CIBLE", "Pas assez de signaux"

def score_text(text: str) -> int:
    return classify_result(text, "", "")[0]


# ============================================================================
# DÉDUPLICATION (version intelligente de ton début)
# ============================================================================

def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower().replace("www.", "")
    path = re.sub(r"/+$", "", (parsed.path or "").lower())
    return f"{host}{path}"


def normalize_title(title: str) -> str:
    t = _norm(title)
    t = re.sub(r"\b(h/f|f/h|m/f)\b", "", t)
    t = re.sub(r"\b(freelance|mission|offre d'emploi|emploi)\b", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def dedup_key_for_record(record: dict) -> str:
    title = normalize_title(record.get("objet", ""))
    location = _norm(record.get("dept", ""))
    return f"{title}|{location}"


def deduplicate_records(records: list[dict]) -> list[dict]:
    by_url = {}
    for r in records:
        key = normalize_url(r.get("url_avis", "")) or f"id:{r.get('id','')}"
        if key not in by_url:
            rr = dict(r)
            rr["_sources"] = [r.get("source", "")]
            rr["_urls"] = [r.get("url_avis", "")]
            by_url[key] = rr
        else:
            current = by_url[key]
            if r.get("score", 0) > current.get("score", 0):
                rr = dict(r)
                rr["_sources"] = current.get("_sources", []) + [r.get("source", "")]
                rr["_urls"] = current.get("_urls", []) + [r.get("url_avis", "")]
                by_url[key] = rr
            else:
                current["_sources"] = current.get("_sources", []) + [r.get("source", "")]
                current["_urls"] = current.get("_urls", []) + [r.get("url_avis", "")]

    by_title = {}
    for r in by_url.values():
        k = dedup_key_for_record(r)
        if k not in by_title:
            by_title[k] = r
            continue

        current = by_title[k]
        current["_sources"] = sorted(set(current.get("_sources", []) + r.get("_sources", [])))
        current["_urls"] = list(dict.fromkeys(current.get("_urls", []) + r.get("_urls", [])))

        cur_len = len(current.get("description", ""))
        new_len = len(r.get("description", ""))
        if (r.get("score", 0), new_len) > (current.get("score", 0), cur_len):
            by_title[k] = r

    out = list(by_title.values())
    for r in out:
        r["sources"] = ", ".join(s for s in r.get("_sources", []) if s)
        r["duplicate_count"] = len(r.get("_urls", []))
    return out


# ============================================================================
# BOAMP
# ============================================================================

def fetch_boamp_records() -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=BOAMP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
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
            print("ℹ 0 résultat BOAMP : normal possible sur 7 jours pour ce sujet.")

        all_results.extend(results)
        if len(results) < BOAMP_PAGE_SIZE:
            break
        offset += BOAMP_PAGE_SIZE

    return all_results


def _flatten_to_text(value) -> str:
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
        v = fields.get(key)
        t = _flatten_to_text(v).strip()
        if t:
            return t
    return ""


def parse_boamp_record(raw: dict) -> dict | None:
    try:
        fields = raw.get("record", {}).get("fields", raw) if isinstance(raw.get("record"), dict) else raw

        objet = _first_nonempty(fields, "objet", "titre") or "Objet non renseigné"
        acheteur = _first_nonempty(fields, "nomacheteur", "nom_acheteur", "acheteur") or "Acheteur non précisé"
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
            "description": description,
        }
    except Exception as exc:
        print(f"⚠ Enregistrement BOAMP ignoré (parsing) : {exc}", file=sys.stderr)
        return None


# ============================================================================
# WEB / EMPLOI / FREELANCE / LINKEDIN (Serper)
# ============================================================================

def fetch_market_results() -> list[dict]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("→ SERPER_API_KEY absente : marché ignoré.")
        return []

    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    results = []

    for query in MARKET_SEARCH_QUERIES:
        payload = {"q": query, "num": MARKET_RESULTS_LIMIT, "gl": "fr", "hl": "fr"}
        try:
            resp = requests.post(SERPER_API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"✗ Erreur marché « {query} » : {exc}", file=sys.stderr)
            continue

        organic = data.get("organic", [])
        print(f"  · marché « {query} » → {len(organic)} résultat(s)")

        for item in organic:
            url = item.get("link", "")
            if not url:
                continue
            title = item.get("title", "Résultat")
            snippet = item.get("snippet", "")
            score, category, reason = classify_result(title, snippet, url)

            results.append({
                "id": f"market:{normalize_url(url) or url}",
                "source": "Emploi/freelance",
                "objet": title,
                "acheteur": "",
                "dept": "",
                "date_parution": "",
                "url_avis": url,
                "score": score,
                "description": snippet,
                "category": category,
                "reason": reason,
            })

        time.sleep(0.35)

    return results


def fetch_linkedin_results() -> list[dict]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("→ SERPER_API_KEY absente : LinkedIn ignoré.")
        return []

    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    results = []

    for query in LINKEDIN_SEARCH_QUERIES:
        payload = {"q": query, "num": LINKEDIN_RESULTS_LIMIT, "gl": "fr", "hl": "fr"}
        try:
            resp = requests.post(SERPER_API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            print(f"✗ Erreur LinkedIn « {query} » : {exc}", file=sys.stderr)
            continue

        organic = data.get("organic", [])
        print(f"  · LinkedIn « {query} » → {len(organic)} résultat(s) bruts")

        for item in organic:
            url = item.get("link", "")
            if not url:
                continue
            host = (urlparse(url).netloc or "").lower()
            if "linkedin.com" not in host:
                continue

            title = item.get("title", "Résultat LinkedIn")
            snippet = item.get("snippet", "")
            score, category, reason = classify_result(title, snippet, url)

            results.append({
                "id": f"linkedin:{normalize_url(url) or url}",
                "source": "LinkedIn",
                "objet": title,
                "acheteur": "",
                "dept": "",
                "date_parution": "",
                "url_avis": url,
                "score": score,
                "description": snippet,
                "category": category,
                "reason": reason,
            })

        time.sleep(0.4)

    return results


# ============================================================================
# HISTORIQUE
# ============================================================================

def load_seen_ids() -> set[str]:
    if SEEN_IDS_FILE.exists():
        try:
            return set(json.loads(SEEN_IDS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            print("⚠ seen_ids.json invalide : réinitialisation.")
    return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_IDS_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================================
# EMAIL
# ============================================================================

def build_email_body(new_matches: list[dict]) -> str:
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y à %H:%M")

    lines = [
        f"Radar Hardis WMS (ex Reflex) — {len(new_matches)} nouvel(aux) résultat(s) détecté(s)",
        f"Généré le {generated_at}",
        "",
    ]

    sorted_matches = sorted(new_matches, key=lambda x: (-x["score"], x["source"]))

    for match in sorted_matches:
        lines.append(f"[{match['score']}/5] [{match['source']}] {match['objet']}")
        if match.get("description"):
            lines.append(f"  {match['description']}")
        if match.get("reason"):
            lines.append(f"  Classification : {match.get('category','')} — {match['reason']}")
        if match.get("url_avis"):
            lines.append(f"  Lien : {match['url_avis']}")
        if match.get("duplicate_count", 1) > 1:
            lines.append(f"  Détecté sur {match.get('duplicate_count')} publication(s)")
        lines.append("")

    return "\n".join(lines)


def send_email(body: str, subject: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("ALERT_EMAIL_FROM", user)
    recipients = [a.strip() for a in os.environ["ALERT_EMAIL_TO"].split(",")]

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
    raw_boamp = fetch_boamp_records()
    boamp_records = [r for r in (parse_boamp_record(rec) for rec in raw_boamp) if r is not None]
    print(f"→ {len(boamp_records)} résultats BOAMP récupérés.")

    print("→ Recherche marché emploi/freelance (Serper)…")
    market_records = fetch_market_results()
    print(f"→ {len(market_records)} résultats marché récupérés.")

    print("→ Recherche LinkedIn (Serper)…")
    linkedin_records = fetch_linkedin_results()
    print(f"→ {len(linkedin_records)} résultats LinkedIn récupérés.")

    all_records = deduplicate_records(boamp_records + market_records + linkedin_records)

    relevant = [r for r in all_records if r.get("score", 0) >= MIN_SCORE_FOR_ALERT]
    print(f"→ {len(relevant)} résultats pertinents (seuil {MIN_SCORE_FOR_ALERT}/5) sur {len(all_records)}.")

    seen_ids = load_seen_ids()
    new_matches = [r for r in relevant if r["id"] not in seen_ids]

    if not new_matches:
        print("→ Aucun nouveau résultat depuis le dernier scan.")
        save_seen_ids(seen_ids | {r["id"] for r in relevant})
        return 0

    print(f"→ {len(new_matches)} nouvel(aux) résultat(s). Envoi de l'email…")
    subject = f"[Radar Hardis WMS] {len(new_matches)} nouvel(aux) résultat(s)"
    body = build_email_body(new_matches)

    send_email(body, subject)
    print("✓ Email envoyé.")

    save_seen_ids(seen_ids | {r["id"] for r in relevant})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
