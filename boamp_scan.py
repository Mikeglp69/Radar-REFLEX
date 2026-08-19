#!/usr/bin/env python3
"""
boamp_scan.py — Veille Hardis WMS (ex Reflex)
                 sur BOAMP + marché emploi/freelance

Sources :
    - BOAMP : API officielle DILA (marchés publics)
    - LinkedIn / Indeed / Mindquest / Hardis : résultats publics indexés via moteur de recherche (Serper)

NOTE MARCHÉ :
    Hardis a rebaptisé son produit « Hardis WMS (ex Reflex) ». Les besoins
    publics autour de Reflex/WMS apparaissent surtout sous forme de missions
    freelance ou d'offres d'emploi (Mindquest, Indeed, Hardis, LinkedIn), et
    beaucoup moins sous forme d'appels d'offres BOAMP. Par conséquent, un
    « 0 résultat BOAMP » sur 7 jours est considéré comme un résultat normal
    et non comme une anomalie à signaler.

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

    MARKET_RESULTS_LIMIT       (défaut 20)
                               Nombre max de résultats par requête marché
                               emploi/freelance (Serper).

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
MARKET_RESULTS_LIMIT = int(os.environ.get("MARKET_RESULTS_LIMIT", "20"))

BOAMP_LOOKBACK_DAYS = int(os.environ.get("BOAMP_LOOKBACK_DAYS", "7"))

BOAMP_DEBUG = os.environ.get("BOAMP_DEBUG") == "1"

# L'API opendatasoft v2.1 plafonne "limit" à 100 par requête : on pagine.
BOAMP_PAGE_SIZE = 100
BOAMP_MAX_PAGES = 20  # garde-fou : 2000 avis max par run


# ============================================================================
# RECHERCHE
# ============================================================================

# Le marché est désormais partagé entre :
#   - marchés publics (BOAMP),
#   - emploi direct,
#   - missions freelance / régie,
#   - cabinets / plateformes qui republient les mêmes missions.
#
# IMPORTANT : ne pas limiter les résultats aux domaines "connus".
# Les plateformes republient fréquemment les mêmes missions.

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
    '"Hardis WMS" consultant France',
    '"Hardis WMS" emploi France',
    '"Hardis WMS" "ex Reflex" France',
    '"Reflex WMS" France',
    '"Reflex WMS" freelance France',
    '"Reflex WMS" consultant France',
    '"Reflex WMS" mission France',
    '"Consultant Reflex WMS" France',
    '"Consultant Expert Reflex WMS" France',
    '"Consultant technico-fonctionnel Reflex" France',
    '"Chef de projet Reflex WMS" France',
    '"Chef de projet WMS Reflex" France',
    '"Expert Reflex WMS" France',
    '"Responsable d\'application WMS REFLEX" France',
    '"support" "Reflex WMS" France',
    '"paramétrage" "Reflex WMS" France',
    '"déploiement" "Reflex WMS" France',
    '"migration" "Reflex WMS" France',
    '"Hardis" WMS freelance',
    '"Hardis" Reflex mission freelance',
    'site:free-work.com "Reflex WMS"',
    'site:freelance-informatique.fr Reflex WMS',
    'site:indeed.com "Reflex WMS"',
    'site:linkedin.com/jobs "Reflex WMS"',
    'site:linkedin.com/jobs "Hardis WMS"',
    'site:hardis-group.com "Consultant WMS"',
    'site:collective.work "Reflex WMS"',
    'site:katchme.fr "Reflex WMS"',
    'site:mindquest.io "Reflex WMS"',
]

LINKEDIN_SEARCH_QUERIES = [
    'site:linkedin.com/jobs/view ("Reflex WMS" OR "Hardis WMS" OR "Consultant Reflex")',
    'site:linkedin.com/posts ("Reflex WMS" OR "Hardis WMS" OR "Consultant Reflex")',
]

# Nombre de résultats maximum demandés à Serper par requête.
MARKET_RESULTS_LIMIT = int(os.environ.get("MARKET_RESULTS_LIMIT", "20"))

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
    "business analyst",
    "amoa",
    "support",
    "paramétrage",
    "integration",
    "intégration",
    "déploiement",
    "migration",
    "mise en production",
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
    "renouvelable",
]

EMPLOYMENT_PHRASES = [
    "cdi",
    "cdd",
    "permanent",
    "permanent employment",
    "contrat à durée indéterminée",
    "recrutement",
    "poste",
    "salaire",
]

LOW_SIGNAL_PHRASES = [
    "logistique",
    "entrepôt",
    "entrepôts",
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
    """
    Retourne :
      score 0..5,
      classe commerciale,
      raison courte.

    Classes :
      - MISSION_FREELANCE : priorité maximale
      - BESOIN_PROJET     : besoin client / projet identifiable
      - RECRUTEMENT       : recrutement direct chez l'éditeur / intégrateur
      - INFORMATION       : contenu WMS pertinent mais moins commercial
      - HORS_CIBLE
    """
    t = _norm(" ".join([title, snippet, url]))

    high = sum(1 for p in HIGH_SIGNAL_PHRASES if _contains_phrase(t, p))
    roles = sum(1 for p in ROLE_PHRASES if _contains_phrase(t, p))
    mission = sum(1 for p in MISSION_PHRASES if _contains_phrase(t, p))
    employment = sum(1 for p in EMPLOYMENT_PHRASES if _contains_phrase(t, p))
    low = sum(1 for p in LOW_SIGNAL_PHRASES if _contains_phrase(t, p))

    if high >= 1 and mission >= 1:
        return 5, "MISSION_FREELANCE", "Reflex/Hardis WMS + signaux de mission freelance"

    if high >= 1 and roles >= 1 and mission >= 1:
        return 5, "MISSION_FREELANCE", "Expertise Reflex/Hardis WMS et mission"

    if high >= 1 and roles >= 1:
        return 4, "BESOIN_PROJET", "Besoin opérationnel clairement lié à Reflex/Hardis WMS"

    if high >= 1 and employment >= 1:
        return 3, "RECRUTEMENT", "Recrutement direct autour de Reflex/Hardis WMS"

    if high >= 1:
        return 3, "INFORMATION", "Mention directe de Reflex/Hardis WMS"

    if low >= 2 and roles >= 1:
        return 2, "INFORMATION", "WMS/logistique avec rôle fonctionnel identifié"

    return 0, "HORS_CIBLE", "Pas de signal Reflex/Hardis WMS suffisamment précis"


def score_text(text: str) -> int:
    return classify_result(text, "", "")[0]


# ============================================================================
# DÉDUPLICATION INTELLIGENTE
# ============================================================================

def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = re.sub(r"/+$", "", parsed.path.lower())
    # Les paramètres de tracking ne doivent jamais créer un faux doublon.
    return f"{host}{path}"


def normalize_title(title: str) -> str:
    t = _norm(title)
    t = re.sub(r"\b(h/f|f/h|m/f)\b", "", t)
    t = re.sub(r"\b(freelance|mission|offre d'emploi|emploi)\b", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def dedup_key(record: dict) -> str:
    title = normalize_title(record.get("objet", ""))
    location = _norm(record.get("dept", ""))
    return f"{title}|{location}"


def deduplicate_records(records: list[dict]) -> list[dict]:
    """
    Déduplication en 2 passes :
      1. URL canonique exacte ;
      2. titre normalisé + localisation.

    Si plusieurs plateformes republient la même mission, on conserve le
    résultat ayant le meilleur score et on agrège les sources/URLs.
    """
    by_url = {}
    for r in records:
        key = normalize_url(r.get("url_avis", ""))
        if not key:
            key = f"id:{r.get('id', '')}"

        if key not in by_url:
            r = dict(r)
            r["_sources"] = [r.get("source", "")]
            r["_urls"] = [r.get("url_avis", "")]
            by_url[key] = r
        else:
            current = by_url[key]
            if r.get("score", 0) > current.get("score", 0):
                r = dict(r)
                r["_sources"] = current.get("_sources", []) + [r.get("source", "")]
                r["_urls"] = current.get("_urls", []) + [r.get("url_avis", "")]
                by_url[key] = r

    by_title = {}
    for r in by_url.values():
        key = dedup_key(r)
        if key not in by_title:
            by_title[key] = r
            continue

        current = by_title[key]
        current["_sources"] = sorted(set(
            current.get("_sources", []) + r.get("_sources", [])
        ))
        current["_urls"] = list(dict.fromkeys(
            current.get("_urls", []) + r.get("_urls", [])
        ))

        # On conserve la fiche la plus riche / la mieux scorée.
        current_text_len = len(current.get("description", ""))
        new_text_len = len(r.get("description", ""))
        if (r.get("score", 0), new_text_len) > (current.get("score", 0), current_text_len):
            r["_sources"] = current["_sources"]
            r["_urls"] = current["_urls"]
            by_title[key] = r

    output = list(by_title.values())

    for r in output:
        r["sources"] = ", ".join(s for s in r.get("_sources", []) if s)
        r["duplicate_count"] = len(r.get("_urls", []))

    return output


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
                "ℹ 0 résultat BOAMP sur la fenêtre courante : situation possible et non bloquante. "
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
# RECHERCHE WEB / EMPLOI / FREELANCE
# ============================================================================

def fetch_market_results() -> list[dict]:
    """
    Recherche large via Serper. Contrairement à l'ancienne version, on ne
    whitelist aucun domaine : une mission peut être publiée sur une
    plateforme, un cabinet, un agrégateur ou le site de l'employeur.
    """
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("→ SERPER_API_KEY absente : recherche Web emploi/freelance ignorée.")
        return []

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    results = []
    seen_urls = set()

    queries = MARKET_SEARCH_QUERIES + LINKEDIN_SEARCH_QUERIES

    for query in queries:
        payload = {
            "q": query,
            "num": MARKET_RESULTS_LIMIT,
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
            print(f"✗ Erreur recherche Web : {exc}", file=sys.stderr)
            continue

        organic = data.get("organic", [])
        print(f"  · requête « {query} » → {len(organic)} résultat(s)")

        for item in organic:
            url = item.get("link", "")
            if not url:
                continue

            canonical = normalize_url(url)
            if canonical in seen_urls:
                continue
            seen_urls.add(canonical)

            title = item.get("title", "Résultat Web")
            snippet = item.get("snippet", "")

            score, category, reason = classify_result(title, snippet, url)

            results.append({
                "title": title,
                "snippet": snippet,
                "url": url,
                "score": score,
                "category": category,
                "reason": reason,
            })

        time.sleep(0.35)

    return results


def parse_market_record(raw: dict) -> dict:
    title = raw.get("title", "Résultat Web")
    snippet = raw.get("snippet", "")
    url = raw.get("url", "")
    score = raw.get("score", 0)

    return {
        "id": f"web:{normalize_url(url) or url}",
        "source": "Web/Emploi",
        "objet": title,
        "acheteur": "",
        "dept": "",
        "date_parution": "",
        "url_avis": url,
        "score": score,
        "description": snippet,
        "category": raw.get("category", "INFORMATION"),
        "reason": raw.get("reason", ""),
    }


# Compatibilité avec l'ancien nom de fonction.
fetch_linkedin_results = fetch_market_results


def parse_linkedin_record(raw: dict) -> dict:
    return parse_market_record(raw)


# ============================================================================
# DÉDUPLICATION INTELLIGENTE
# ============================================================================

def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = re.sub(r"/+$", "", parsed.path.lower())
    # Les paramètres de tracking ne doivent jamais créer un faux doublon.
    return f"{host}{path}"


def normalize_title(title: str) -> str:
    t = _norm(title)
    t = re.sub(r"\b(h/f|f/h|m/f)\b", "", t)
    t = re.sub(r"\b(freelance|mission|offre d'emploi|emploi)\b", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def dedup_key(record: dict) -> str:
    title = normalize_title(record.get("objet", ""))
    location = _norm(record.get("dept", ""))
    return f"{title}|{location}"


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
                "ℹ 0 résultat BOAMP sur la fenêtre courante : situation possible et non bloquante. "
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


def fetch_market_results() -> list[dict]:
    """Recherche large emploi/freelance via Serper.

    On ne limite PAS la collecte à trois domaines : les offres Reflex/WMS
    sont souvent syndiquées ou publiées sur des plateformes comme Free-Work,
    Collective.work, Freelance-Informatique, KatchMe, Indeed, etc.
    """
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("✗ SERPER_API_KEY absente : impossible d'interroger le marché emploi/freelance.")
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
            print(f"✗ Erreur recherche marché « {query} » : {exc}", file=sys.stderr)
            continue

        organic = data.get("organic", [])
        print(f"  · marché « {query} » → {len(organic)} résultat(s) Serper")

        for item in organic:
            url = item.get("link", "")
            if not url:
                continue
            results.append({
                "title": item.get("title", "Résultat marché emploi/freelance"),
                "snippet": item.get("snippet", ""),
                "url": url,
            })
        time.sleep(0.4)

    return results

def parse_market_record(raw: dict) -> dict:
    """Transforme un résultat emploi/freelance en format commun."""
    title = raw.get("title", "Résultat marché emploi/freelance")
    snippet = raw.get("snippet", "")
    url = raw.get("url", "")
    return {
        "id": f"market:{url}",
        "source": "Emploi/freelance",
        "objet": title,
        "acheteur": "",
        "dept": "",
        "date_parution": "",
        "url_avis": url,
        "score": score_text(" ".join([title, snippet])),
        "description": snippet,
    }


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
        f"Radar Hardis WMS (ex Reflex) — {len(new_matches)} nouvel(aux) résultat(s) détecté(s)",
        f"Généré le {generated_at}",
        "",
        "Contexte marché : Hardis WMS (ex Reflex) est principalement visible dans les besoins emploi/freelance. Un 0 résultat BOAMP sur 7 jours est donc normal et ne constitue pas, à lui seul, un bug.",
        
    ]

    sorted_matches = sorted(
        new_matches, key=lambda x: (-x["score"], x["source"])
    )

    for match in sorted_matches:
        lines.append(
            f"[{match['score']}/5] [{match['source']}] [{match.get('category', '')}] {match['objet']}"
        )

        if match["source"] == "BOAMP":
            if match["acheteur"]:
                line = f"  Acheteur : {match['acheteur']}"
                if match["dept"]:
                    line += f" (dépt. {match['dept']})"
                lines.append(line)

            if match["date_parution"]:
                lines.append(f"  Publié le : {match['date_parution']}")

        elif match["source"] in ("LinkedIn", "Emploi/freelance"):
            if match.get("description"):
                lines.append(f"  {match['description']}")

        if match.get("reason"):
            lines.append(f"  Classification : {match.get('category', 'INFORMATION')} — {match['reason']}")
        if match.get("duplicate_count", 1) > 1:
            lines.append(
                f"  Détecté sur {match['duplicate_count']} publication(s)/URL(s) "
                f"({match.get('sources', '')})"
            )
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

    print("→ Recherche Web / emploi / freelance…")
    market_raw = fetch_market_results()
    market_records = [parse_market_record(r) for r in market_raw]
    print(f"→ {len(market_records)} résultats Web/emploi récupérés.")

    print("→ Recherche emploi/freelance (Mindquest / Indeed / Hardis)…")
    market_raw = fetch_market_results()
    market_records = [parse_market_record(r) for r in market_raw]
    print(f"→ {len(market_records)} résultats emploi/freelance récupérés.")

    if not boamp_records:
        print("ℹ 0 résultat BOAMP : situation possible et normale sur ce marché ; les besoins sont surtout suivis côté emploi/freelance.")

    all_records = deduplicate_records(
        boamp_records + linkedin_records + market_records
    )

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
        f"[Radar Hardis WMS] {len(new_matches)} nouvel(aux) résultat(s) BOAMP / emploi-freelance / LinkedIn"
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
