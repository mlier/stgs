import csv
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://abonne.stgs.fr/wp")
LOGIN    = os.getenv("LOGIN")
PASSWORD = os.getenv("PASSWORD")

DATA_DIR         = os.getenv("DATA_DIR", "./data")
SEUIL_JOURNALIER = float(os.getenv("SEUIL_JOURNALIER", "0"))

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_LOGIN    = os.getenv("SMTP_LOGIN")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM    = os.getenv("EMAIL_FROM")
EMAIL_TO      = [e.strip() for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()]

DAILY_CSV_COLUMNS = ["date", "periode", "total", "Télérelève", "Fuite en cours", "Fraude", "unite", "mise à jour"]


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

class _FormParser(HTMLParser):
    """Parse un formulaire HTML pour en extraire l'action et les champs input."""

    def __init__(self):
        super().__init__()
        self.form_action = None
        self.fields = {}
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_form = True
            self.form_action = attrs.get("action", "")
        if tag == "input" and self.in_form:
            name = attrs.get("name")
            if name:
                self.fields[name] = (attrs.get("type", "text"), attrs.get("value", ""))


def login(session: requests.Session) -> bool:
    """Authentifie la session sur le portail Agence en Ligne.

    Récupère la page de login, parse dynamiquement le formulaire HTML (action
    Struts avec jsessionid, champs login/password), soumet les credentials et
    vérifie la présence d'un lien de déconnexion dans la réponse.

    Args:
        session: Session requests à authentifier (les cookies sont mis à jour en place).

    Returns:
        True si l'authentification a réussi, False sinon.
    """
    r = session.get(f"{BASE_URL}/login.action", allow_redirects=True)
    login_url = r.url

    parser = _FormParser()
    parser.feed(r.text)

    post_data = {n: v for n, (t, v) in parser.fields.items() if t == "hidden"}
    for name, (ftype, _) in parser.fields.items():
        if ftype in ("text", "email") or "login" in name.lower() or "user" in name.lower():
            post_data[name] = LOGIN
        if ftype == "password" or "pass" in name.lower() or "pwd" in name.lower():
            post_data[name] = PASSWORD

    action = parser.form_action or "login.action"
    if action.startswith("http"):
        submit_url = action
    elif action.startswith("/"):
        base = urlparse(login_url)
        submit_url = f"{base.scheme}://{base.netloc}{action}"
    else:
        submit_url = f"{BASE_URL}/{action}"

    r = session.post(submit_url, data=post_data, allow_redirects=True)
    return "logout" in r.text or "déconnexion" in r.text


# ---------------------------------------------------------------------------
# Récupération des données
# ---------------------------------------------------------------------------

def get_data(session: requests.Session) -> dict:
    """Récupère les données de consommation depuis l'endpoint AJAX du portail.

    Appelle chartsTeleMeasure.action avec les headers AJAX requis. La session
    doit être préalablement authentifiée via login().

    Args:
        session: Session requests authentifiée.

    Returns:
        Dictionnaire JSON contenant les données de consommation pour toutes les
        granularités (gd/gw/gm/gy), structuré par type de fluide (ex: "EAU").

    Raises:
        requests.HTTPError: Si le serveur retourne un statut HTTP d'erreur.
        requests.JSONDecodeError: Si la réponse n'est pas du JSON valide
            (indique généralement une session non authentifiée).
    """
    r = session.get(
        f"{BASE_URL}/chartsTeleMeasure.action",
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{BASE_URL}/chartTeleMeasurePage.action",
        },
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Répertoire de données
# ---------------------------------------------------------------------------

def get_data_dir() -> Path:
    """Retourne le répertoire de données, en le créant si nécessaire.

    Returns:
        Chemin absolu du répertoire DATA_DIR (créé avec mkdir -p si absent).
    """
    path = Path(DATA_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# CSV snapshot (toutes granularités, horodaté)
# ---------------------------------------------------------------------------

GRANULARITY_LABELS = {
    "gy": "annuel",
    "gm": "mensuel",
    "gw": "hebdomadaire",
    "gd": "journalier",
}


def _label_to_iso(label: str, granularity: str) -> str:
    """Convertit un label de date du portail vers le format ISO.

    L'année est inférée depuis la date courante : si le mois du label est
    supérieur au mois courant, le label appartient à l'année précédente.

    Args:
        label: Label brut retourné par le serveur ("21/05", "S 11/05",
               "05/2026", "2025").
        granularity: Granularité parmi "journalier", "hebdomadaire",
                     "mensuel", "annuel".

    Returns:
        "YYYY-MM-DD" pour journalier et hebdomadaire,
        "YYYY-MM" pour mensuel, "YYYY" pour annuel.
    """
    today = datetime.now()

    if granularity == "journalier":      # "21/05" → "2026-05-21"
        d, m = int(label[:2]), int(label[3:5])
        y = today.year if m <= today.month else today.year - 1
        return f"{y}-{m:02d}-{d:02d}"

    if granularity == "hebdomadaire":    # "S 11/05" → "2026-05-11"
        d, m = int(label[2:4]), int(label[5:7])
        y = today.year if m <= today.month else today.year - 1
        return f"{y}-{m:02d}-{d:02d}"

    if granularity == "mensuel":         # "05/2026" → "2026-05-01"
        m, y = int(label[:2]), int(label[3:])
        return f"{y}-{m:02d}-01"

    return label                         # "2025" → "2025"


def _build_snapshot_rows(data: dict) -> list[dict]:
    """Convertit le JSON brut du portail en liste de lignes CSV normalisées.

    Args:
        data: Dictionnaire JSON retourné par get_data().

    Returns:
        Liste de dicts avec les clés : type, granularite, periode, unite, total,
        et une colonne par label (Télérelève, Fuite en cours, Fraude).
    """
    rows = []
    for type_key, type_data in data.items():
        if not isinstance(type_data, dict):
            continue
        nature = type_data.get("nature", type_key)
        col_labels = [l.rstrip(" :") for l in type_data.get("labels", [])]
        for granu_key, granu_data in type_data.items():
            if not isinstance(granu_data, dict) or "xdatas" not in granu_data:
                continue
            granularite = GRANULARITY_LABELS.get(granu_key, granu_key)
            unity = granu_data.get("unity", "")
            for entry in granu_data["xdatas"]:
                values, label, total = entry[0], entry[1], entry[2]
                row = {"type": nature, "granularite": granularite,
                       "periode": _label_to_iso(label, granularite), "unite": unity, "total": total}
                for i, val in enumerate(values):
                    row[col_labels[i] if i < len(col_labels) else f"valeur_{i}"] = val
                rows.append(row)
    return rows


def save_snapshot(data: dict, filepath: Path):
    """Sauvegarde un snapshot complet (toutes granularités) dans un CSV horodaté.

    Args:
        data: Dictionnaire JSON retourné par get_data().
        filepath: Chemin du fichier CSV à créer (ex: data/conso_20260522_083000.csv).
    """
    rows = _build_snapshot_rows(data)
    if rows:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Snapshot sauvegardé : {filepath}")


def find_latest_snapshot(data_dir: Path) -> Path | None:
    """Retourne le snapshot CSV le plus récent dans le répertoire de données.

    Args:
        data_dir: Répertoire contenant les fichiers conso_YYYYMMDD_HHMMSS.csv.

    Returns:
        Chemin du snapshot le plus récent, ou None si aucun n'existe.
    """
    snapshots = sorted(data_dir.glob("conso_????????_??????.csv"))
    return snapshots[-1] if snapshots else None



def data_changed(new_data: dict, last_snapshot: Path) -> bool:
    """Compare les nouvelles données avec le dernier snapshot pour détecter un changement.

    Toutes les valeurs sont normalisées en str avant comparaison pour éviter
    les faux positifs entre valeurs numériques JSON (int/float) et chaînes CSV.

    Args:
        new_data: Dictionnaire JSON fraîchement récupéré depuis le portail.
        last_snapshot: Chemin du CSV snapshot le plus récent.

    Returns:
        True si les données diffèrent du snapshot (un nouveau snapshot doit être écrit).
    """
    def normalize(r):
        return tuple(sorted((k, str(v)) for k, v in r.items()))
    new_rows = {normalize(r) for r in _build_snapshot_rows(new_data)}
    with open(last_snapshot, newline="", encoding="utf-8") as f:
        old_rows = {normalize(r) for r in csv.DictReader(f)}
    return new_rows != old_rows


# ---------------------------------------------------------------------------
# CSV incrémental journalier
# ---------------------------------------------------------------------------

def update_incremental_csv(daily_entries: list, filepath: Path) -> tuple[int, int]:
    """Ajoute ou met à jour les entrées journalières dans le CSV incrémental.

    Insère les nouvelles périodes et écrase les existantes si la valeur a changé.
    Met à jour le timestamp "mise à jour" à chaque insertion ou modification.
    Les dates sont stockées au format ISO "YYYY-MM-DD".

    Args:
        daily_entries: Liste d'entrées xdatas journalières au format
            [[telereleve, fuite, fraude], "JJ/MM", total] (format serveur).
        filepath: Chemin vers conso_quotidienne.csv.

    Returns:
        Tuple (ajouts, mises_à_jour) comptant les lignes créées et modifiées.
    """
    existing = {}
    if filepath.exists():
        with open(filepath, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["periode"]] = row

    col_names = ["Télérelève", "Fuite en cours", "Fraude"]
    added, updated = 0, 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for entry in daily_entries:
        values, label, total = entry[0], entry[1], entry[2]
        iso_label = _label_to_iso(label, "journalier")
        real_date = (datetime.strptime(iso_label, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        new_row = {"date": real_date, "periode": iso_label, "total": total, "unite": "Litre",
                   "mise à jour": now}
        for i, col in enumerate(col_names):
            new_row[col] = values[i] if i < len(values) else 0.0
        if iso_label not in existing:
            existing[iso_label] = new_row
            added += 1
        elif float(existing[iso_label]["total"]) != float(total):
            existing[iso_label] = new_row
            updated += 1

    if added == 0 and updated == 0:
        return added, updated

    rows = sorted(existing.values(), key=lambda r: r["periode"])
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return added, updated


# ---------------------------------------------------------------------------
# Seuil et alertes
# ---------------------------------------------------------------------------

def get_last_completed_day(daily_entries: list) -> tuple[str, float] | None:
    """Retourne le dernier jour complété (total > 0) autre que la date du jour.

    Le jour courant est exclu car sa consommation peut être partielle.

    Args:
        daily_entries: Liste d'entrées xdatas journalières au format
            [[telereleve, fuite, fraude], "JJ/MM", total] (format serveur).

    Returns:
        Tuple (label_date, total_litres) du dernier jour complété, ou None.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for entry in reversed(daily_entries):
        _, label, total = entry[0], entry[1], entry[2]
        iso_label = _label_to_iso(label, "journalier")
        if total > 0 and iso_label != today:
            return iso_label, total
    return None


def check_alert_already_sent(alert_file: Path, date_label: str) -> bool:
    """Vérifie si une alerte a déjà été envoyée pour cette date.

    Args:
        alert_file: Fichier .last_alert contenant la dernière date alertée.
        date_label: Label de date au format ISO "YYYY-MM-DD".

    Returns:
        True si l'alerte pour cette date a déjà été envoyée.
    """
    if not alert_file.exists():
        return False
    return alert_file.read_text().strip() == date_label


def mark_alert_sent(alert_file: Path, date_label: str):
    """Enregistre la date pour laquelle une alerte vient d'être envoyée.

    Args:
        alert_file: Fichier .last_alert à mettre à jour.
        date_label: Label de date au format ISO "YYYY-MM-DD".
    """
    alert_file.write_text(date_label)


def send_alert_email(csv_path: Path, date_label: str, valeur: float, seuil: float):
    """Envoie un email d'alerte avec le CSV en pièce jointe via SMTP Gmail.

    Args:
        csv_path: Chemin du CSV à joindre (dernier snapshot ou conso_quotidienne.csv).
        date_label: Label de la date concernée au format "JJ/MM".
        valeur: Consommation journalière en litres.
        seuil: Seuil configuré en litres (SEUIL_JOURNALIER).
    """
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(EMAIL_TO)
    msg["Subject"] = (
        f"[ALERTE EAU] Consommation {valeur:.0f}L le {date_label}"
        f" — seuil {seuil:.0f}L dépassé"
    )

    body = (
        f"Alerte consommation d'eau\n\n"
        f"Date            : {date_label}\n"
        f"Consommation    : {valeur:.0f} Litres\n"
        f"Seuil configuré : {seuil:.0f} Litres\n\n"
        f"L'historique complet est joint à cet email."
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(csv_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={csv_path.name}")
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_LOGIN, SMTP_PASSWORD)
        smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"  Email d'alerte envoyé à : {', '.join(EMAIL_TO)}")


# ---------------------------------------------------------------------------
# Histogrammes et rapport email
# ---------------------------------------------------------------------------

def build_charts(csv_path: Path, data_dir: Path, timestamp: str) -> list[Path]:
    """Génère les histogrammes de consommation depuis conso_quotidienne.csv.

    Produit jusqu'à 4 PNG horodatés dans data_dir :
    hist_15j_YYYYMMDD_HHMMSS.png, etc. Un graphique est omis si les données
    sont insuffisantes.

    Args:
        csv_path: Chemin vers conso_quotidienne.csv.
        data_dir: Répertoire de sortie pour les PNG.
        timestamp: Horodatage au format YYYYMMDD_HHMMSS (partagé avec le snapshot).

    Returns:
        Liste des chemins PNG effectivement créés.
    """
    if not csv_path.exists():
        return []

    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["date"])
    df["total"] = df["total"].astype(float)
    df = df.sort_values("date").reset_index(drop=True)

    images: list[Path] = []

    def _save(fig, path: Path) -> Path:
        plt.tight_layout()
        plt.savefig(path, dpi=100)
        plt.close(fig)
        return path

    # 15 derniers jours (min 3 valeurs > 0)
    last15 = df.tail(15)
    if (last15["total"] > 0).sum() >= 3:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(last15["date"].dt.strftime("%d/%m"), last15["total"], color="steelblue")
        ax.set_ylabel("Litres")
        ax.set_title("Consommation — 15 derniers jours")
        plt.xticks(rotation=45, ha="right")
        images.append(_save(fig, data_dir / f"hist_15j_{timestamp}.png"))

    # Semaines (min 1, max 56)
    weekly = df.groupby(df["date"].dt.to_period("W"))["total"].sum().tail(56)
    if len(weekly) >= 1:
        labels = [p.start_time.strftime("%d/%m/%y") for p in weekly.index]
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(labels, weekly.values, color="steelblue")
        ax.set_ylabel("Litres")
        ax.set_title("Consommation hebdomadaire")
        plt.xticks(rotation=45, ha="right")
        images.append(_save(fig, data_dir / f"hist_semaines_{timestamp}.png"))

    # Mois (min 1, max 24)
    monthly = df.groupby(df["date"].dt.to_period("M"))["total"].sum().tail(24)
    if len(monthly) >= 1:
        labels = monthly.index.strftime("%m/%Y")
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(labels, monthly.values, color="steelblue")
        ax.set_ylabel("Litres")
        ax.set_title("Consommation mensuelle")
        plt.xticks(rotation=45, ha="right")
        images.append(_save(fig, data_dir / f"hist_mois_{timestamp}.png"))

    # Années (min 1 entrée)
    yearly = df.groupby(df["date"].dt.to_period("Y"))["total"].sum()
    if len(yearly) >= 1:
        labels = yearly.index.strftime("%Y")
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(labels, yearly.values, color="steelblue")
        ax.set_ylabel("Litres")
        ax.set_title("Consommation annuelle")
        plt.xticks(rotation=45, ha="right")
        images.append(_save(fig, data_dir / f"hist_annees_{timestamp}.png"))

    return images


def send_report_email(images: list[Path], csv_path: Path):
    """Envoie le rapport de consommation par email avec histogrammes en pièces jointes.

    Args:
        images: Liste des chemins PNG à joindre.
        csv_path: Chemin de conso_quotidienne.csv à joindre.
    """
    msg = MIMEMultipart()
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(EMAIL_TO)
    msg["Subject"] = f"[CONSO EAU] Rapport du {datetime.now().strftime('%Y-%m-%d')}"

    body = (
        f"Rapport de consommation d'eau\n\n"
        f"Date     : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Graphiques joints : {len(images)}\n"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for img_path in images:
        with open(img_path, "rb") as f:
            part = MIMEImage(f.read(), "png")
        part.add_header("Content-Disposition", f"attachment; filename={img_path.name}")
        msg.attach(part)

    with open(csv_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={csv_path.name}")
    msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_LOGIN, SMTP_PASSWORD)
        smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

    print(f"  Rapport envoyé à : {', '.join(EMAIL_TO)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    data_dir = get_data_dir()
    csv_path = data_dir / "conso_quotidienne.csv"

    latest = find_latest_snapshot(data_dir)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })

    print("Connexion en cours...")
    if not login(session):
        print("Connexion incertaine — vérifiez login/password dans .env")
    else:
        print("Connecté.")

    data          = get_data(session)
    daily_entries = data.get("EAU", {}).get("gd", {}).get("xdatas", [])

    json_path = data_dir / "data_raw.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON brut sauvegardé : {json_path}")

    # Snapshot conditionnel
    timestamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = data_dir / f"conso_{timestamp}.csv"
    if latest is None or data_changed(data, latest):
        save_snapshot(data, snapshot_path)
        last_csv = snapshot_path
    else:
        print("Données identiques au dernier snapshot — écriture ignorée.")
        last_csv = latest

    # CSV incrémental journalier
    added, updated = update_incremental_csv(daily_entries, csv_path)
    print(f"CSV incrémental mis à jour : {csv_path} ({added} ajout(s), {updated} mise(s) à jour)")

    # Rapport email avec histogrammes
    images = build_charts(csv_path, data_dir, timestamp)
    if images and EMAIL_TO:
        try:
            send_report_email(images, csv_path)
            print(f"  Rapport envoyé ({len(images)} graphique(s)).")
        except Exception as e:
            print(f"  Erreur envoi rapport : {e}")

    # Alerte seuil (désactivée si SEUIL_JOURNALIER=0)
    if SEUIL_JOURNALIER >= 0:
        result = get_last_completed_day(daily_entries)
        if result:
            date_label, valeur = result
            if valeur > SEUIL_JOURNALIER:
                alert_file = data_dir / ".last_alert"
                if check_alert_already_sent(alert_file, date_label):
                    print(f"  Seuil dépassé le {date_label} ({valeur:.0f}L) — alerte déjà envoyée.")
                else:
                    print(f"  Seuil dépassé le {date_label} ({valeur:.0f}L > {SEUIL_JOURNALIER:.0f}L) — envoi email...")
                    try:
                        send_alert_email(last_csv, date_label, valeur, SEUIL_JOURNALIER)
                        mark_alert_sent(alert_file, date_label)
                    except Exception as e:
                        print(f"  Erreur envoi email : {e}")
                        print("  Vérifiez SMTP_PASSWORD dans .env (mot de passe d'application Google requis).")
