import csv
import os
import smtplib
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

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

DAILY_CSV_COLUMNS = ["periode", "total", "Télérelève", "Fuite en cours", "Fraude", "unite"]


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

class _FormParser(HTMLParser):
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


def _build_snapshot_rows(data: dict) -> list[dict]:
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
                       "periode": label, "unite": unity, "total": total}
                for i, val in enumerate(values):
                    row[col_labels[i] if i < len(col_labels) else f"valeur_{i}"] = val
                rows.append(row)
    return rows


def save_snapshot(data: dict, filepath: Path):
    rows = _build_snapshot_rows(data)
    if rows:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Snapshot sauvegardé : {filepath}")


def find_latest_snapshot(data_dir: Path) -> Path | None:
    snapshots = sorted(data_dir.glob("conso_????????_??????.csv"))
    return snapshots[-1] if snapshots else None


def today_in_incremental_csv(csv_path: Path) -> bool:
    if not csv_path.exists():
        return False
    today = datetime.now().strftime("%d/%m")
    with open(csv_path, newline="", encoding="utf-8") as f:
        return any(row["periode"] == today for row in csv.DictReader(f))


def data_changed(new_data: dict, last_snapshot: Path) -> bool:
    new_rows = {tuple(sorted(r.items())) for r in _build_snapshot_rows(new_data)}
    with open(last_snapshot, newline="", encoding="utf-8") as f:
        old_rows = {tuple(sorted(r.items())) for r in csv.DictReader(f)}
    return new_rows != old_rows


# ---------------------------------------------------------------------------
# CSV incrémental journalier
# ---------------------------------------------------------------------------

def _sort_key(label: str) -> tuple:
    parts = label.split("/")
    if len(parts) == 2:
        return (int(parts[1]), int(parts[0]))  # (mois, jour)
    return (0, 0)


def update_incremental_csv(daily_entries: list, filepath: Path) -> int:
    existing = {}
    if filepath.exists():
        with open(filepath, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["periode"]] = row

    col_names = ["Télérelève", "Fuite en cours", "Fraude"]
    added = 0
    for entry in daily_entries:
        values, label, total = entry[0], entry[1], entry[2]
        if label not in existing:
            row = {"periode": label, "total": total, "unite": "Litre"}
            for i, col in enumerate(col_names):
                row[col] = values[i] if i < len(values) else 0.0
            existing[label] = row
            added += 1

    rows = sorted(existing.values(), key=lambda r: _sort_key(r["periode"]))
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DAILY_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return added


# ---------------------------------------------------------------------------
# Seuil et alertes
# ---------------------------------------------------------------------------

def get_last_completed_day(daily_entries: list) -> tuple[str, float] | None:
    today = datetime.now().strftime("%d/%m")
    for entry in reversed(daily_entries):
        _, label, total = entry[0], entry[1], entry[2]
        if total > 0 and label != today:
            return label, total
    return None


def check_alert_already_sent(alert_file: Path, date_label: str) -> bool:
    if not alert_file.exists():
        return False
    return alert_file.read_text().strip() == date_label


def mark_alert_sent(alert_file: Path, date_label: str):
    alert_file.write_text(date_label)


def send_alert_email(csv_path: Path, date_label: str, valeur: float, seuil: float):
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
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    data_dir = get_data_dir()
    csv_path = data_dir / "conso_quotidienne.csv"

    if today_in_incremental_csv(csv_path):
        print("Données du jour déjà présentes dans le CSV — aucun appel au service.")
        raise SystemExit(0)

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
    added = update_incremental_csv(daily_entries, csv_path)
    print(f"CSV incrémental mis à jour : {csv_path} ({added} nouveau(x) jour(s))")

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
