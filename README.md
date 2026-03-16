# sharewood-migrator

CLI pour:

- tirer un cache complet des torrents Sharewood depuis torr9,
- afficher les categories avec le nombre de torrents a sauver (seeders=0 dans le cache),
- synchroniser vers qBittorrent avec filtres, en ajoutant un tracker, uniquement si le torrent est toujours a 0 seeder cote torr9.

## Installation

Prerequis:

- Python 3.12+
- dependances du projet installees (`requests`)
- avant archives des torrents ShareWood

Avec `uv`:

```bash
uv sync
```

## Configuration TOML

Copier l'exemple:

```bash
cp sharewood.toml.example sharewood.toml
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

Les memes cles peuvent aussi etre placees dans une table `[sharewood]`.

## Commandes

### 1) Pull du cache

Telecharge les pages une par une et produit:

- `cache/pages/page_XXXXX.json`
- `cache/aggregated.json`

Comportement cache:

- si une page existe deja dans `cache/pages`, elle est reutilisee (pas de re-download)
- pour forcer le re-download, ajouter `--force`

```bash
uv run sharewood-migrator --config sharewood.toml pull-cache
```

Options utiles:

```bash
uv run sharewood-migrator --config sharewood.toml pull-cache \
  --start-page 0 \
  --page-size 100 \
  --force \
  --pause-seconds 1.0 \
  --timeout 30
```

### 2) Stats categories

Affiche les categories existantes + nombre de torrents a sauver selon `cache/aggregated.json` (seeders=0 dans le cache).

```bash
uv run sharewood-migrator --config sharewood.toml categories
```

### 3) Sync vers qBittorrent

Prend les torrents du cache qui matchent les filtres utilisateur, verifie en live sur torr9 qu'ils sont encore a 0 seeder, puis:

- ajoute le `.torrent` a qBittorrent,
- ajoute le tracker configure.

Lors de l'ajout dans qBittorrent, le CLI applique aussi:

- le tag configure via `qb_add_tag` (par defaut `sharewood-migrator`)
- le dossier de destination configure via `qb_add_save_path` (par defaut `/media/downloads/sharewood-migrator`)

Mode simulation:

```bash
uv run sharewood-migrator --config sharewood.toml sync --dry-run
```

Exemp
les de filtres:

```bash
uv run sharewood-migrator --config sharewood.toml sync \
  --category BD \
  --min-size 1000000 \
  --max-size 1000000000 \
  --name "nicolas" \
  --id 169590 \
  --id-range 160000-170000 \
  --limit 20 \
  --dry-run
```

Sans `--dry-run`, la commande execute le push dans qBittorrent.

## Script entrypoint

Le CLI est expose via:

- `sharewood-migrator` -> `sharewood_cli:main`

Tu peux aussi lancer directement:

```bash
uv run python sharewood_cli.py --config sharewood.toml categories
```
