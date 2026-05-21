# stgs-conso

Extraction automatique des données de consommation d'eau depuis le portail **"Agence en Ligne"** (STGS).

## Prérequis

- [uv](https://docs.astral.sh/uv/) (gestionnaire de paquets Python)
- Un abonnement actif sur le portail `abonne.stgs.fr`

## Installation

```bash
uv sync
```

## Configuration

Créez un fichier `.env` à la racine :

```env
BASE_URL=https://abonne.stgs.fr/wp
LOGIN=votre_email@exemple.fr
PASSWORD=votre_mot_de_passe
```

## Utilisation

```bash
uv run stgs_conso.py
```

Le script se connecte au portail, récupère les données et génère un fichier `conso_YYYYMMDD_HHMMSS.csv`.

## Format du CSV

| Colonne | Description |
|---------|-------------|
| `type` | Nature du fluide (Eau) |
| `granularite` | journalier / hebdomadaire / mensuel / annuel |
| `periode` | Label de la période (ex: `16/05`, `S 11/05`, `05/2026`, `2025`) |
| `unite` | Litre |
| `total` | Consommation totale en litres |
| `Télérelève` | Part télérelève |
| `Fuite en cours` | Part fuite détectée |
| `Fraude` | Part fraude détectée |

## Limites

- Données journalières limitées aux **10 derniers jours** (limite serveur)
- Données mensuelles disponibles sur les **12 derniers mois**
- Données annuelles disponibles sur les **3 dernières années**

## Compatibilité

Le portail "Agence en Ligne" est utilisé par plusieurs distributeurs d'eau français. Le script est compatible avec tout opérateur sur cette plateforme en modifiant `BASE_URL` dans le `.env`.
