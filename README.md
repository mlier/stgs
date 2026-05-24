# Suivi de la consommation d'eau du founisseur STGS

Extraction automatique des données de consommation d'eau depuis le portail **"Agence en Ligne"** (STGS).

## Fonctionnalités

- **Authentification automatique** — connexion au portail via parsing dynamique du formulaire HTML (compatible avec le système de tokens Struts)
- **Extraction multi-granularité** — récupération des données sur 4 périodes : journalier (10 derniers jours), hebdomadaire, mensuel (12 mois), annuel (3 ans)
- **Snapshot CSV horodaté** — sauvegarde dans `conso_YYYYMMDD_HHMMSS.csv` uniquement si les données ont changé depuis la dernière lecture
- **CSV incrémental journalier** — `conso_quotidienne.csv` maintenu à jour à chaque exécution (nouvelles entrées ajoutées, corrections appliquées)
- **Alertes email** — notification si la consommation d'un jour dépasse le seuil configuré (`SEUIL_JOURNALIER`), sans doublon d'envoi
- **Rapport périodique par email** — histogrammes de consommation sur 4 horizons (15 jours, semaines, mois, années) envoyés en pièces jointes
- **Logging** — messages horodatés sur la console et dans un fichier optionnel (`LOG_FILE`)

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
| `date` | Date réelle de consommation (= `periode` − 1 jour) |
| `type` | Nature du fluide (Eau) |
| `granularite` | journalier / hebdomadaire / mensuel / annuel |
| `periode` | Label J-1 affiché sur le portail (ex: `2026-05-22`) |
| `unite` | Litre |
| `total` | Consommation totale en litres |
| `Télérelève` | Part télérelève |
| `Fuite en cours` | Part fuite détectée |
| `Fraude` | Part fraude détectée |
| `mise à jour` | Date et heure ISO de la dernière modification de la ligne |

## Calendrier de remontée des données

Le portail fonctionne avec un décalage de 2 jours :

| Jour | Rôle |
|------|------|
| J-2  | Consommation réelle mesurée |
| J-1  | Label affiché dans le CSV (`periode`) |
| J    | Transmission sur le portail (généralement l'après-midi) |

Exemple : le 23 mai, la ligne `periode=2026-05-22` contient la consommation
du 21 mai (`date=2026-05-21`). Sa valeur est transmise dans l'après-midi du 23.

Les valeurs d'une période passée peuvent être corrigées par le portail à tout
moment. Le script interroge donc systématiquement le portail à chaque exécution.
Il ne crée ou modifie des fichiers que si les données ont réellement changé
depuis la dernière lecture — aucune écriture si tout est identique.

## Limites

- Données journalières limitées aux **10 derniers jours** (limite serveur)
- Données mensuelles disponibles sur les **12 derniers mois**
- Données annuelles disponibles sur les **3 dernières années**

## Compatibilité

Le portail "Agence en Ligne" est utilisé par plusieurs distributeurs d'eau français. Le script est compatible avec tout opérateur sur cette plateforme en modifiant `BASE_URL` dans le `.env`.
