# File objective

This file provides guidance to coding systems when working with code in this repository.

## Commands

```bash
uv run stgs_conso.py          # exécution principale
uv add <package>              # ajouter une dépendance
```

## Configuration

Credentials et URL dans `.env` (non versionné) :
```
BASE_URL=https://abonne.stgs.fr/wp
LOGIN=...
PASSWORD=...
```

Le portail cible est **"Agence en Ligne"** de STGS — plateforme Struts (Java) utilisée par plusieurs distributeurs d'eau français. Changer `BASE_URL` suffit pour cibler un autre opérateur sur la même plateforme.

## Architecture

`stgs_conso.py` est le seul fichier source. Il s'organise en trois blocs :

1. **`login(session)`** — récupère la page de login, parse dynamiquement le formulaire HTML (action Struts avec jsessionid, champs `j_username`/`j_password`), soumet et vérifie la connexion.

2. **`get_data(session)`** — appelle `chartsTeleMeasure.action` avec les headers AJAX requis (`X-Requested-With: XMLHttpRequest`). Renvoie du JSON directement sans passer par la page HTML du graphique.

3. **`save_csv(data, filepath)`** — itère sur toutes les granularités présentes dans le JSON (`gd`/`gw`/`gm`/`gy`) et écrit un CSV multi-granularité horodaté.

## Structure du JSON retourné

```
{
  "EAU": {
    "nature": "Eau",
    "labels": ["Télérelève :", "Fuite en cours :", "Fraude :"],
    "gd": { "label": "derniers jours",    "nbPlages": 10, "unity": "Litre", "xdatas": [...] },
    "gw": { "label": "dernières semaines","nbPlages": 4,  "unity": "Litre", "xdatas": [...] },
    "gm": { "label": "derniers mois",     "nbPlages": 12, "unity": "Litre", "xdatas": [...] },
    "gy": { "label": "dernières années",  "nbPlages": 3,  "unity": "Litre", "xdatas": [...] }
  }
}
```

Chaque entrée `xdatas` : `[[telereleve, fuite, fraude], "label_date", total]`

**Limite connue** : `gd` est plafonné à 10 jours par le serveur, sans paramètre de pagination disponible.
