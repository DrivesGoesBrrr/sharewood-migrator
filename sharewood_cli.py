from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

try:
    import tomllib
except ModuleNotFoundError as error:  # pragma: no cover
    raise RuntimeError("Python 3.11+ is required to read TOML config files") from error


TORR9_SHAREWOOD_API_URL = "https://api.torr9.net/api/v1/torrents/sharewood"
TORR9_TORRENT_API_URL = "https://api.torr9.net/api/v1/torrents/{torrent_id}"
TORR9_TORRENT_HTML_URL = "https://torr9.net/torrents/{torrent_id}"
CACHE_AGGREGATED_FILENAME = "aggregated.json"
CACHE_PAGES_DIRNAME = "pages"


@dataclass(frozen=True)
class AppConfig:
    torr9_jwt: str
    qbittorrent_url: str
    tracker_url: str
    sharewood_archive_dir: Path
    cache_dir: Path
    qb_add_tag: str
    qb_add_save_path: str
    qb_username: str | None = None
    qb_password: str | None = None


def _config_table(raw: dict[str, Any]) -> dict[str, Any]:
    table = raw.get("sharewood")
    if isinstance(table, dict):
        return table
    return raw


def _require_non_empty_string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid config key: {key}")
    return value.strip()


def _optional_non_empty_string(table: dict[str, Any], key: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid config key: {key}")
    return value.strip()


def normalize_qb_url(qb_url: str) -> str:
    value = qb_url.strip()
    if not value:
        raise ValueError("qBittorrent URL cannot be empty")

    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"

    parsed = urlparse(value)
    if not parsed.netloc:
        raise ValueError(f"Invalid qBittorrent URL: {qb_url}")

    return value.rstrip("/")


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid TOML structure")

    table = _config_table(raw)

    torr9_jwt = _require_non_empty_string(table, "torr9_jwt")
    qbittorrent_url = normalize_qb_url(_require_non_empty_string(table, "qbittorrent_url"))
    tracker_url = _require_non_empty_string(table, "tracker_url")
    sharewood_archive_dir = Path(_require_non_empty_string(table, "sharewood_archive_dir"))
    cache_dir = Path(_require_non_empty_string(table, "cache_dir"))
    qb_add_tag = table.get("qb_add_tag", "sharewood-migrator")
    if not isinstance(qb_add_tag, str) or not qb_add_tag.strip():
        raise ValueError("Invalid config key: qb_add_tag")

    qb_add_save_path = table.get("qb_add_save_path", "/media/downloads/sharewood-migrator")
    if not isinstance(qb_add_save_path, str) or not qb_add_save_path.strip():
        raise ValueError("Invalid config key: qb_add_save_path")

    qb_username = _optional_non_empty_string(table, "qb_username")
    qb_password = _optional_non_empty_string(table, "qb_password")

    return AppConfig(
        torr9_jwt=torr9_jwt,
        qbittorrent_url=qbittorrent_url,
        tracker_url=tracker_url,
        sharewood_archive_dir=sharewood_archive_dir,
        cache_dir=cache_dir,
        qb_add_tag=qb_add_tag.strip(),
        qb_add_save_path=qb_add_save_path.strip(),
        qb_username=qb_username,
        qb_password=qb_password,
    )


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("items", "data", "results", "torrents"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]

    return []


def _has_next_page(payload: Any, current_page: int) -> bool | None:
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("has_next"), bool):
        return payload["has_next"]

    if isinstance(payload.get("next_page"), int):
        return payload["next_page"] > current_page

    total_pages = payload.get("total_pages")
    if isinstance(total_pages, int):
        return current_page < total_pages

    pagination = payload.get("pagination")
    if isinstance(pagination, dict):
        if isinstance(pagination.get("has_next"), bool):
            return pagination["has_next"]
        if isinstance(pagination.get("next_page"), int):
            return pagination["next_page"] > current_page
        if isinstance(pagination.get("total_pages"), int):
            return current_page < pagination["total_pages"]

    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _aggregated_cache_path(config: AppConfig) -> Path:
    return config.cache_dir / CACHE_AGGREGATED_FILENAME


def _pages_dir_path(config: AppConfig) -> Path:
    return config.cache_dir / CACHE_PAGES_DIRNAME


def pull_cache(
    config: AppConfig,
    start_page: int,
    page_size: int,
    pause_seconds: float,
    timeout: int,
    force: bool,
) -> None:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = _pages_dir_path(config)
    pages_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "Accept": "*/*",
        "Authorization": f"Bearer {config.torr9_jwt}",
        "Origin": "https://torr9.net",
        "Referer": "https://torr9.net/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3.1 Safari/605.1.15"
        ),
    }

    all_items: list[dict[str, Any]] = []
    page = start_page

    while True:
        page_file = pages_dir / f"page_{page:05d}.json"
        if page_file.exists() and not force:
            payload = json.loads(page_file.read_text(encoding="utf-8"))
            print(f"Using cached page={page}: {page_file}")
        else:
            query = urlencode({"page": page, "limit": page_size})
            response = requests.get(
                f"{TORR9_SHAREWOOD_API_URL}?{query}",
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            page_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Downloaded page={page} -> {page_file}")

        items = _extract_items(payload)
        if not items:
            print(f"Stop page={page}: no item returned")
            break

        all_items.extend(items)
        print(f"Page={page} items={len(items)} total={len(all_items)}")

        has_next = _has_next_page(payload, page)
        if has_next is False:
            print("Stop: API reports no next page")
            break

        page += 1
        if pause_seconds > 0:
            time.sleep(pause_seconds)

    aggregate_path = _aggregated_cache_path(config)
    aggregate_path.write_text(json.dumps(all_items, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Cache written: {aggregate_path} ({len(all_items)} torrents)")


def load_cached_torrents(config: AppConfig) -> list[dict[str, Any]]:
    pages_dir = _pages_dir_path(config)
    page_files = sorted(pages_dir.glob("page_*.json")) if pages_dir.exists() else []

    # Prefer page cache when present: it is usually the most complete source.
    if page_files:
        torrents_from_pages: list[dict[str, Any]] = []
        for page_file in page_files:
            try:
                payload = json.loads(page_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            torrents_from_pages.extend(_extract_items(payload))

        if torrents_from_pages:
            return torrents_from_pages

    aggregate_path = _aggregated_cache_path(config)
    if not aggregate_path.exists():
        raise FileNotFoundError(
            f"Cache not found: {aggregate_path}. Run 'pull-cache' first."
        )

    payload = json.loads(aggregate_path.read_text(encoding="utf-8"))

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        torrents = payload.get("torrents")
        if isinstance(torrents, list):
            return [item for item in torrents if isinstance(item, dict)]

    return []


def _is_zero_from_cache(item: dict[str, Any]) -> bool:
    return _as_int(item.get("seeders")) == 0


def print_categories(config: AppConfig) -> None:
    torrents = load_cached_torrents(config)
    total_counter: Counter[str] = Counter()
    zero_counter: Counter[str] = Counter()

    for item in torrents:
        category_name = item.get("category_name")
        if not isinstance(category_name, str) or not category_name.strip():
            category_name = "<unknown>"
        normalized = category_name.strip()
        total_counter[normalized] += 1
        if _is_zero_from_cache(item):
            zero_counter[normalized] += 1

    total_torrents = len(torrents)
    total_zero = sum(zero_counter.values())

    print(f"Torrents in cache: {total_torrents}")
    print(f"Torrents to rescue (seeders=0): {total_zero}")
    if not total_counter:
        print("No category found.")
        return

    print("\nCategories:")
    for category_name, total_count in sorted(
        total_counter.items(),
        key=lambda pair: (-pair[1], pair[0].lower()),
    ):
        rescue_count = zero_counter.get(category_name, 0)
        print(f"{category_name}: total={total_count} rescue={rescue_count}")


def normalize_infohash(infohash: str) -> str:
    normalized = infohash.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ValueError("infohash must be a 40-character hexadecimal string")
    return normalized


def _parse_bencode(data: bytes, index: int) -> tuple[Any, int, bytes | None]:
    token = data[index : index + 1]

    if token == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1, None

    if token == b"l":
        index += 1
        output: list[Any] = []
        info_bytes: bytes | None = None
        while data[index : index + 1] != b"e":
            value, index, child_info = _parse_bencode(data, index)
            output.append(value)
            if info_bytes is None and child_info is not None:
                info_bytes = child_info
        return output, index + 1, info_bytes

    if token == b"d":
        index += 1
        output: dict[bytes, Any] = {}
        info_bytes: bytes | None = None
        while data[index : index + 1] != b"e":
            key_obj, index, _ = _parse_bencode(data, index)
            if not isinstance(key_obj, bytes):
                raise ValueError("Invalid bencode dictionary key")

            value_start = index
            value_obj, index, child_info = _parse_bencode(data, index)
            output[key_obj] = value_obj

            if key_obj == b"info":
                info_bytes = data[value_start:index]
            elif info_bytes is None and child_info is not None:
                info_bytes = child_info

        return output, index + 1, info_bytes

    if token.isdigit():
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end, None

    raise ValueError("Invalid bencode token")


def extract_infohash_from_torrent_file(torrent_file: Path) -> str:
    raw = torrent_file.read_bytes()
    _, end_index, info_bytes = _parse_bencode(raw, 0)

    if end_index != len(raw):
        raise ValueError("Trailing bytes after valid torrent payload")
    if info_bytes is None:
        raise ValueError("Torrent payload does not contain an info dictionary")

    return hashlib.sha1(info_bytes).hexdigest()


def _extract_seeders_from_html(html: str) -> int | None:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html, "html.parser")

    for label_node in soup.find_all(string=re.compile(r"^\s*seeders?\s*$", re.IGNORECASE)):
        label_element = label_node.parent
        if label_element is None:
            continue

        parent = label_element.parent
        if parent is not None:
            for child in parent.find_all(recursive=False):
                child_text = child.get_text(" ", strip=True)
                if re.fullmatch(r"\d+", child_text):
                    return int(child_text)

        prev_sibling = label_element.find_previous_sibling()
        if prev_sibling is not None:
            sibling_text = prev_sibling.get_text(" ", strip=True)
            match = re.search(r"(\d+)", sibling_text)
            if match:
                return int(match.group(1))

    text = soup.get_text(" ", strip=True).lower()
    for pattern in (r"seeders?\s*[:\-]?\s*(\d+)", r"(\d+)\s*seeders?"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))

    for node in soup.find_all(string=re.compile(r"seeder", re.IGNORECASE)):
        nearby = node.parent.get_text(" ", strip=True) if node.parent else str(node)
        match = re.search(r"(\d+)", nearby)
        if match:
            return int(match.group(1))

    return None


def check_torrent_is_zero_seeder(torrent_id: int, token: str, timeout: int) -> tuple[bool, int | None]:
    html_headers = {
        "Accept": "text/html,*/*",
        "Authorization": f"Bearer {token}",
        "Origin": "https://torr9.net",
        "Referer": "https://torr9.net/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3.1 Safari/605.1.15"
        ),
    }

    html_response = requests.get(
        TORR9_TORRENT_HTML_URL.format(torrent_id=torrent_id),
        headers=html_headers,
        timeout=timeout,
    )
    html_response.raise_for_status()

    seeders = _extract_seeders_from_html(html_response.text)
    if seeders is not None:
        return seeders == 0, seeders

    api_response = requests.get(
        TORR9_TORRENT_API_URL.format(torrent_id=torrent_id),
        headers={
            "Accept": "application/json,*/*",
            "Authorization": f"Bearer {token}",
            "Origin": "https://torr9.net",
            "Referer": "https://torr9.net/",
            "User-Agent": html_headers["User-Agent"],
        },
        timeout=timeout,
    )
    api_response.raise_for_status()

    payload = api_response.json()
    api_seeders = payload.get("seeders") if isinstance(payload, dict) else None

    if isinstance(api_seeders, int):
        return api_seeders == 0, api_seeders
    if isinstance(api_seeders, str) and api_seeders.strip().isdigit():
        parsed = int(api_seeders.strip())
        return parsed == 0, parsed

    return False, None


def qb_login(
    session: requests.Session,
    qb_url: str,
    username: str | None,
    password: str | None,
    timeout: int,
) -> None:
    if username is None and password is None:
        return

    if not username or not password:
        raise ValueError(
            "Both qb_username and qb_password are required when authentication is enabled"
        )

    response = session.post(
        f"{qb_url}/api/v2/auth/login",
        data={"username": username, "password": password},
        timeout=timeout,
    )
    response.raise_for_status()

    if response.text.strip() != "Ok.":
        raise RuntimeError(f"qBittorrent login failed: {response.text.strip()}")


def qb_find_torrent_hash(
    session: requests.Session,
    qb_url: str,
    torrent_hash: str,
    timeout: int,
) -> str | None:
    target_lower = torrent_hash.lower()
    target_upper = torrent_hash.upper()

    for param_name in ("hashes", "hash"):
        for candidate in (target_lower, target_upper):
            response = session.get(
                f"{qb_url}/api/v2/torrents/info",
                params={param_name: candidate},
                timeout=timeout,
            )
            response.raise_for_status()

            payload = response.json()
            if not isinstance(payload, list):
                continue

            for item in payload:
                if not isinstance(item, dict):
                    continue
                current_hash = item.get("hash")
                if not isinstance(current_hash, str):
                    continue
                normalized = current_hash.strip().lower()
                if normalized == target_lower:
                    return current_hash.strip()

    return None


def qb_add_torrent_file(
    session: requests.Session,
    qb_url: str,
    torrent_file: Path,
    add_tag: str,
    add_save_path: str,
    timeout: int,
) -> None:
    with torrent_file.open("rb") as file_handle:
        response = session.post(
            f"{qb_url}/api/v2/torrents/add",
            files={"torrents": (torrent_file.name, file_handle, "application/x-bittorrent")},
            data={
                "tags": add_tag,
                "savepath": add_save_path,
            },
            timeout=timeout,
        )
    response.raise_for_status()

    body = response.text.strip()
    if body and body.lower() in {"fails.", "fail", "error"}:
        raise RuntimeError(f"Failed to add torrent to qBittorrent: {body}")


def qb_wait_for_torrent_hash(
    session: requests.Session,
    qb_url: str,
    torrent_hash: str,
    timeout: int,
    max_wait_seconds: int = 20,
) -> str | None:
    target_lower = torrent_hash.lower()
    target_upper = torrent_hash.upper()

    for _ in range(max_wait_seconds):
        for candidate in (target_lower, target_upper):
            response = session.get(
                f"{qb_url}/api/v2/torrents/info",
                params={"hashes": candidate},
                timeout=timeout,
            )
            response.raise_for_status()

            payload = response.json()
            if isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, dict):
                    current_hash = first.get("hash")
                    if isinstance(current_hash, str) and current_hash.strip():
                        return current_hash.strip()

        time.sleep(1)

    return None


def _normalize_tracker_url(url: str) -> str:
    return url.strip().rstrip("/")


def qb_has_tracker(
    session: requests.Session,
    qb_url: str,
    torrent_hash: str,
    tracker_url: str,
    timeout: int,
) -> bool:
    target = _normalize_tracker_url(tracker_url)

    for hash_value in (torrent_hash, torrent_hash.lower(), torrent_hash.upper()):
        try:
            response = session.get(
                f"{qb_url}/api/v2/torrents/trackers",
                params={"hash": hash_value},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            continue

        payload = response.json()
        if not isinstance(payload, list):
            continue

        for item in payload:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if isinstance(url, str) and _normalize_tracker_url(url) == target:
                return True

    return False


def qb_add_tracker(
    session: requests.Session,
    qb_url: str,
    torrent_hash: str,
    tracker_url: str,
    timeout: int,
) -> None:
    attempts = [
        {"hashes": torrent_hash, "urls": tracker_url},
        {"hashes": torrent_hash.upper(), "urls": tracker_url},
        {"hashes": torrent_hash, "urls": f"{tracker_url}\n"},
        {"hashes": torrent_hash.upper(), "urls": f"{tracker_url}\n"},
        {"hash": torrent_hash, "urls": tracker_url},
        {"hash": torrent_hash.upper(), "urls": tracker_url},
        {"hash": torrent_hash, "urls": f"{tracker_url}\n"},
        {"hash": torrent_hash.upper(), "urls": f"{tracker_url}\n"},
    ]

    last_error: Exception | None = None
    last_body = ""

    for payload in attempts:
        try:
            response = session.post(
                f"{qb_url}/api/v2/torrents/addTrackers",
                data=payload,
                timeout=timeout,
            )
            last_body = response.text.strip()
            response.raise_for_status()
            return
        except requests.RequestException as error:
            last_error = error

    if last_error is not None:
        if qb_has_tracker(
            session=session,
            qb_url=qb_url,
            torrent_hash=torrent_hash,
            tracker_url=tracker_url,
            timeout=timeout,
        ):
            return

        message = f"Unable to add tracker for hash {torrent_hash}"
        if last_body:
            message = f"{message}: {last_body}"
        raise RuntimeError(message) from last_error


def _parse_id_ranges(values: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for value in values:
        match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", value)
        if not match:
            raise ValueError(f"Invalid id range: {value} (expected START-END)")
        start = int(match.group(1))
        end = int(match.group(2))
        if end < start:
            raise ValueError(f"Invalid id range: {value} (end < start)")
        ranges.append((start, end))
    return ranges


def _id_matches_ranges(value: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= value <= end for start, end in ranges)


def _item_torrent_id(item: dict[str, Any]) -> int | None:
    return _as_int(item.get("id"))


def _item_file_size(item: dict[str, Any]) -> int | None:
    return _as_int(item.get("file_size_bytes"))


def _item_title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, str):
        return title
    return "<no title>"


def _item_infohash(item: dict[str, Any]) -> str | None:
    info_hash = item.get("info_hash")
    if not isinstance(info_hash, str) or not info_hash.strip():
        return None
    return normalize_infohash(info_hash)


def filter_candidates(
    torrents: list[dict[str, Any]],
    categories: list[str],
    min_size: int | None,
    max_size: int | None,
    name_filter: str | None,
    ids: list[int],
    id_ranges: list[tuple[int, int]],
    limit: int | None,
) -> list[dict[str, Any]]:
    category_filter = {value.strip().lower() for value in categories if value.strip()}
    id_filter = set(ids)
    name_filter_lower = name_filter.lower() if name_filter else None

    selected: list[dict[str, Any]] = []

    for item in torrents:
        if not _is_zero_from_cache(item):
            continue

        torrent_id = _item_torrent_id(item)
        info_hash = _item_infohash(item)
        if torrent_id is None or info_hash is None:
            continue

        if category_filter:
            category_name = item.get("category_name")
            if not isinstance(category_name, str) or category_name.strip().lower() not in category_filter:
                continue

        file_size = _item_file_size(item)
        if min_size is not None:
            if file_size is None or file_size < min_size:
                continue
        if max_size is not None:
            if file_size is None or file_size > max_size:
                continue

        if name_filter_lower is not None and name_filter_lower not in _item_title(item).lower():
            continue

        if id_filter and torrent_id not in id_filter:
            continue

        if id_ranges and not _id_matches_ranges(torrent_id, id_ranges):
            continue

        selected.append(item)
        if limit is not None and len(selected) >= limit:
            break

    return selected


def find_matching_torrents_in_archive(
    archive_dir: Path,
    target_infohashes: set[str],
) -> tuple[dict[str, Path], int]:
    if not archive_dir.exists():
        raise FileNotFoundError(f"Archive directory not found: {archive_dir}")

    mapping: dict[str, Path] = {}
    unreadable = 0

    for torrent_file in sorted(archive_dir.rglob("*.torrent")):
        if len(mapping) == len(target_infohashes):
            break

        try:
            infohash = extract_infohash_from_torrent_file(torrent_file)
        except (OSError, ValueError):
            unreadable += 1
            continue

        if infohash in target_infohashes and infohash not in mapping:
            mapping[infohash] = torrent_file

    return mapping, unreadable


def sync_torrents(
    config: AppConfig,
    categories: list[str],
    min_size: int | None,
    max_size: int | None,
    name_filter: str | None,
    ids: list[int],
    id_ranges: list[str],
    limit: int | None,
    dry_run: bool,
    check_timeout: int,
    qb_timeout: int,
) -> None:
    torrents = load_cached_torrents(config)
    parsed_id_ranges = _parse_id_ranges(id_ranges)

    selected = filter_candidates(
        torrents=torrents,
        categories=categories,
        min_size=min_size,
        max_size=max_size,
        name_filter=name_filter,
        ids=ids,
        id_ranges=parsed_id_ranges,
        limit=limit,
    )

    if not selected:
        print("No torrent matched the filters.")
        return

    print(f"Candidates from cache (seeders=0): {len(selected)}")

    wanted_hashes = {
        infohash for item in selected for infohash in [_item_infohash(item)] if infohash is not None
    }
    archive_map, unreadable_files = find_matching_torrents_in_archive(
        archive_dir=config.sharewood_archive_dir,
        target_infohashes=wanted_hashes,
    )

    print(f"Archive matches found: {len(archive_map)} / {len(wanted_hashes)}")
    if unreadable_files:
        print(f"Unreadable .torrent files while indexing archive: {unreadable_files}")

    session = requests.Session()
    if not dry_run:
        qb_login(
            session=session,
            qb_url=config.qbittorrent_url,
            username=config.qb_username,
            password=config.qb_password,
            timeout=qb_timeout,
        )

    added = 0
    already_present = 0
    skipped_not_zero = 0
    skipped_missing_archive = 0
    check_errors = 0
    add_errors = 0

    for item in selected:
        torrent_id = _item_torrent_id(item)
        title = _item_title(item)
        infohash = _item_infohash(item)

        if torrent_id is None or infohash is None:
            continue

        torrent_file = archive_map.get(infohash)
        if torrent_file is None:
            skipped_missing_archive += 1
            print(f"SKIP missing archive | id={torrent_id} | infohash={infohash} | {title}")
            continue

        try:
            still_zero, live_seeders = check_torrent_is_zero_seeder(
                torrent_id=torrent_id,
                token=config.torr9_jwt,
                timeout=check_timeout,
            )
        except requests.RequestException as error:
            check_errors += 1
            print(f"ERROR check live seeders | id={torrent_id} | {error}")
            continue

        if not still_zero:
            skipped_not_zero += 1
            print(
                f"SKIP no longer zero | id={torrent_id} | live_seeders={live_seeders} | {title}"
            )
            continue

        if dry_run:
            print(
                "DRY-RUN add | "
                f"id={torrent_id} | infohash={infohash} | seeders={live_seeders} | file={torrent_file} | {title}"
            )
            continue

        try:
            visible_hash = qb_find_torrent_hash(
                session=session,
                qb_url=config.qbittorrent_url,
                torrent_hash=infohash,
                timeout=qb_timeout,
            )

            hash_for_tracker = visible_hash
            if hash_for_tracker is None:
                qb_add_torrent_file(
                    session=session,
                    qb_url=config.qbittorrent_url,
                    torrent_file=torrent_file,
                    add_tag=config.qb_add_tag,
                    add_save_path=config.qb_add_save_path,
                    timeout=qb_timeout,
                )
                visible_hash = qb_wait_for_torrent_hash(
                    session=session,
                    qb_url=config.qbittorrent_url,
                    torrent_hash=infohash,
                    timeout=qb_timeout,
                )
                hash_for_tracker = visible_hash or infohash
                added += 1
                print(f"ADDED torrent | id={torrent_id} | hash={hash_for_tracker} | {title}")
            else:
                already_present += 1
                print(f"ALREADY in qB | id={torrent_id} | hash={hash_for_tracker} | {title}")

            if qb_has_tracker(
                session=session,
                qb_url=config.qbittorrent_url,
                torrent_hash=hash_for_tracker,
                tracker_url=config.tracker_url,
                timeout=qb_timeout,
            ):
                print(f"TRACKER already present | id={torrent_id}")
            else:
                qb_add_tracker(
                    session=session,
                    qb_url=config.qbittorrent_url,
                    torrent_hash=hash_for_tracker,
                    tracker_url=config.tracker_url,
                    timeout=qb_timeout,
                )
                print(f"TRACKER added | id={torrent_id}")

        except (requests.RequestException, RuntimeError, ValueError) as error:
            add_errors += 1
            print(f"ERROR add to qB | id={torrent_id} | {error}")

    print("\nSummary")
    print(f"Selected from cache: {len(selected)}")
    print(f"Added to qBittorrent: {added}")
    print(f"Already present in qBittorrent: {already_present}")
    print(f"Skipped (not zero anymore): {skipped_not_zero}")
    print(f"Skipped (archive missing): {skipped_missing_archive}")
    print(f"Live check errors: {check_errors}")
    print(f"Add/track errors: {add_errors}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sharewood-migrator",
        description="CLI tool to cache torr9 data and sync zero-seeder torrents to qBittorrent.",
    )
    parser.add_argument(
        "--config",
        default="sharewood.toml",
        help="TOML config path (default: sharewood.toml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    pull_parser = subparsers.add_parser(
        "pull-cache",
        help="Download all Sharewood pages and build an aggregated cache file.",
    )
    pull_parser.add_argument("--start-page", type=int, default=0, help="First page number (default: 0)")
    pull_parser.add_argument("--page-size", type=int, default=100, help="API page size (default: 100)")
    pull_parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if page files already exist in cache.",
    )
    pull_parser.add_argument(
        "--pause-seconds",
        type=float,
        default=1.0,
        help="Pause between pages in seconds (default: 1.0)",
    )
    pull_parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)")

    subparsers.add_parser(
        "categories",
        help="Print categories and counts of torrents to rescue (seeders=0 in cache).",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        help="Sync filtered zero-seeder torrents to qBittorrent and add tracker.",
    )
    sync_parser.add_argument("--category", action="append", default=[], help="Filter category_name")
    sync_parser.add_argument("--min-size", type=int, help="Minimum file_size_bytes")
    sync_parser.add_argument("--max-size", type=int, help="Maximum file_size_bytes")
    sync_parser.add_argument("--name", help="Case-insensitive substring on torrent title")
    sync_parser.add_argument("--id", action="append", type=int, default=[], help="Exact torrent id filter")
    sync_parser.add_argument(
        "--id-range",
        action="append",
        default=[],
        help="Inclusive id range START-END (can be repeated)",
    )
    sync_parser.add_argument("--limit", type=int, help="Maximum number of selected torrents")
    sync_parser.add_argument("--dry-run", action="store_true", help="Print actions without adding anything")
    sync_parser.add_argument(
        "--check-timeout",
        type=int,
        default=30,
        help="HTTP timeout for torr9 seeder checks (default: 30)",
    )
    sync_parser.add_argument(
        "--qb-timeout",
        type=int,
        default=30,
        help="HTTP timeout for qBittorrent requests (default: 30)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(Path(args.config))

    if args.command == "pull-cache":
        pull_cache(
            config=config,
            start_page=args.start_page,
            page_size=args.page_size,
            pause_seconds=args.pause_seconds,
            timeout=args.timeout,
            force=args.force,
        )
        return

    if args.command == "categories":
        print_categories(config)
        return

    if args.command == "sync":
        sync_torrents(
            config=config,
            categories=args.category,
            min_size=args.min_size,
            max_size=args.max_size,
            name_filter=args.name,
            ids=args.id,
            id_ranges=args.id_range,
            limit=args.limit,
            dry_run=args.dry_run,
            check_timeout=args.check_timeout,
            qb_timeout=args.qb_timeout,
        )
        return

    parser.error("Unknown command")


if __name__ == "__main__":
    main()
