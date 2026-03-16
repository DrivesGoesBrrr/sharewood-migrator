# sharewood-migrator

CLI pour:

- tirer un cache complet des torrents Sharewood depuis torr9,
- afficher les categories avec le nombre de torrents a sauver (seeders=0 dans le cache),
- synchroniser vers qBittorrent avec filtres, en ajoutant un tracker, uniquement si le torrent est toujours a 0 seeder cote torr9.

## Installation

Prerequis:

- Python 3.12+
- dependances du projet installees (`requests`, `beautifulsoup4`)

Avec `uv`:

```bash
uv sync
```

Sans `uv` (avec `pip`):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install requests
```

Execution du tool avec `pip`:

Remplacer `uv run sharewood-migrator` par `python sharewood_cli.py`

## Configuration TOML

Copier l'exemple:

```bash
cp config.toml.example config.toml
```

Puis renseigner:

```toml
torr9_jwt = "PUT_YOUR_TORR9_JWT_HERE"
qbittorrent_url = "http://127.0.0.1:8080"
tracker_url = "https://tracker.torr9.net/announce/abcdef1234567890"
sharewood_archive_dir = "SharewoodArchive"
cache_dir = "cache"
qb_add_tag = "sharewood-migrator"
qb_add_save_path = "/media/downloads/sharewood-migrator"

# Optionnel si auth qB active
qb_username = "admin"
qb_password = "adminadmin"
```

### Recuperer le JWT torr9 (`torr9_jwt`)

Pour remplir `torr9_jwt` dans `config.toml`:

1. Va sur la page d'accueil de torr9 et connecte-toi.
2. Ouvre les outils de developpement avec `F12`.
3. Va dans l'onglet de stockage/cookies (`Application` ou `Storage` selon le navigateur).
4. Ouvre les cookies du domaine torr9.
5. Recupere la valeur du cookie `token`.
6. Colle cette valeur dans `torr9_jwt`.

Exemple:

```toml
torr9_jwt = "PASTE_COOKIE_TOKEN_HERE"
```

Note: ce token donne acces a ton compte torr9. Ne le partage jamais.

### Recuperer et configurer l'archive Sharewood

Pour que le tool puisse retrouver les `.torrent` a ajouter, il faut d'abord recuperer l'archive Sharewood:

1. Telecharge sur torr9 le torrent `Sharewood.Archive.2026.zip`.
2. Une fois le download termine, extrais le `.zip`.
3. Repere le dossier extrait (celui qui contient l'arborescence de l'archive).
4. Renseigne ce chemin dans `config.toml` via `sharewood_archive_dir`.

Exemple:

```toml
sharewood_archive_dir = "/chemin/vers/SharewoodArchive"
```

Le chemin peut etre absolu ou relatif au dossier du projet.

## Commandes

Liste rapide:

- `pull-cache`: telecharge/reutilise les pages de cache et regenere `aggregated.json`
- `categories`: affiche les categories avec `total` et `rescue` (seeders=0)
- `sync`: ajoute vers qBittorrent les torrents filtres encore a 0 seeder sur torr9
- `fix-trackers`: ajoute en masse le tracker torr9 pour les torrents qB ayant deja un tracker `sharewood.tv`

### 1) Pull du cache

Telecharge les pages une par une et produit:

- `cache/pages/page_XXXXX.json`
- `cache/aggregated.json`

Comportement cache:

- si une page existe deja dans `cache/pages`, elle est reutilisee (pas de re-download)
- pour forcer le re-download, ajouter `--force`

Flags:

- `--start-page` page de depart (defaut: `0`)
- `--page-size` taille de page API (defaut: `100`)
- `--pause-seconds` pause entre les pages (defaut: `1.0`)
- `--timeout` timeout HTTP en secondes (defaut: `30`)
- `--force` force le re-download meme si les pages existent

Exemple (valeurs par defaut):

```bash
uv run sharewood-migrator --config config.toml pull-cache \
  --start-page 0 \
  --page-size 100 \
  --pause-seconds 1.0 \
  --timeout 30
```

### 2) Stats categories

Affiche les categories existantes avec:

- `total`: nombre total de torrents dans la categorie
- `rescue`: nombre de torrents avec `seeders=0`

La commande lit en priorite `cache/pages/page_*.json` (cache complet), puis `cache/aggregated.json` en fallback.

Flags:

- aucun flag specifique (en dehors de `--config` global)

Exemple (valeurs par defaut):

```bash
uv run sharewood-migrator --config config.toml categories
```

### 3) Sync vers qBittorrent

Prend les torrents du cache qui matchent les filtres utilisateur, verifie en live sur torr9 qu'ils sont encore a 0 seeder, puis:

- ajoute le `.torrent` a qBittorrent,
- ajoute le tracker configure.

Lors de l'ajout dans qBittorrent, le CLI applique aussi:

- le tag configure via `qb_add_tag` (par defaut `sharewood-migrator`)
- le dossier de destination configure via `qb_add_save_path` (par defaut `/media/downloads/sharewood-migrator`)

Flags:

- `--category` filtre sur `category_name` (repetable)
- `--min-size` taille minimale en bytes
- `--max-size` taille maximale en bytes
- `--name` sous-chaine (insensible a la casse) sur le titre
- `--id` filtre par id exact (repetable)
- `--id-range` filtre par plage inclusive `START-END` (repetable)
- `--limit` nombre maximal de torrents a traiter
- `--dry-run` simule sans ajouter dans qBittorrent
- `--check-timeout` timeout HTTP pour le check torr9 (defaut: `30`)
- `--qb-timeout` timeout HTTP pour qBittorrent (defaut: `30`)

Exemple (valeurs par defaut):

```bash
uv run sharewood-migrator --config config.toml sync \
  --check-timeout 30 \
  --qb-timeout 30
```

Sans `--dry-run`, la commande execute le push dans qBittorrent.

### 4) Fix trackers en masse

Ajoute le tracker torr9 configure (`tracker_url`) sur tous les torrents deja presents dans qBittorrent qui ont un tracker `sharewood.tv`, utile quand certains ajouts de tracker ont echoue.

Flags:

- `--dry-run` simule sans ajouter le tracker
- `--qb-timeout` timeout HTTP pour qBittorrent (defaut: `30`)

Exemple (valeurs par defaut):

```bash
uv run sharewood-migrator --config config.toml fix-trackers --qb-timeout 30
```
