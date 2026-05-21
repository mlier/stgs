import requests
import json
import csv
import os
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "https://abonne.stgs.fr/wp")
LOGIN    = os.getenv("LOGIN")
PASSWORD = os.getenv("PASSWORD")

GRANULARITY_LABELS = {
    "gy": "annuel",
    "gm": "mensuel",
    "gw": "hebdomadaire",
    "gd": "journalier",
}


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


def save_csv(data: dict, filepath: str):
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
                row = {
                    "type":        nature,
                    "granularite": granularite,
                    "periode":     label,
                    "unite":       unity,
                    "total":       total,
                }
                for i, val in enumerate(values):
                    col = col_labels[i] if i < len(col_labels) else f"valeur_{i}"
                    row[col] = val
                rows.append(row)

    if not rows:
        print("Aucune donnée à sauvegarder.")
        return

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV sauvegardé : {filepath} ({len(rows)} lignes)")


if __name__ == "__main__":
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })

    print("Connexion en cours...")
    if not login(session):
        print("Connexion incertaine — vérifiez login/password dans .env")
    else:
        print("Connecté.")

    data = get_data(session)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_csv(data, f"conso_{timestamp}.csv")
