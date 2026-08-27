#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from yt_dlp import YoutubeDL

CHANNEL_HANDLE = os.getenv("CHANNEL_HANDLE", "mariaspeaksenglish").lstrip("@")
CHANNEL_URL = f"https://www.youtube.com/@{CHANNEL_HANDLE}"
TABS = {
    "Vídeo": f"{CHANNEL_URL}/videos",
    "Short": f"{CHANNEL_URL}/shorts",
    "Directo": f"{CHANNEL_URL}/streams",
}

RECENT_KEEP = int(os.getenv("RECENT_KEEP", "75"))
BACKFILL_BATCH = int(os.getenv("BACKFILL_BATCH", "100"))
BACKFILL_HOLD_HOURS = int(os.getenv("BACKFILL_HOLD_HOURS", "24"))

# Exact metadata enrichment. On each run we visit a limited number of watch
# pages and cache the real upload date in catalog.json. At 90/run and one run
# every 6 h, a ~300-video channel normally becomes fully dated within a day.
DATE_ENRICH_BATCH = int(os.getenv("DATE_ENRICH_BATCH", "90"))
DATE_RETRY_HOURS = int(os.getenv("DATE_RETRY_HOURS", "18"))
DATE_ENRICH_SLEEP = float(os.getenv("DATE_ENRICH_SLEEP", "0.20"))

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "catalog.json"
STATE_PATH = ROOT / "backfill_state.json"
FEED_PATH = ROOT / "feed.xml"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def flatten_entries(entries: Iterable[Any]) -> Iterable[dict[str, Any]]:
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("entries")
        if nested:
            yield from flatten_entries(nested)
        elif entry.get("id"):
            yield entry


def entry_datetime(entry: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "release_timestamp", "modified_timestamp"):
        value = entry.get(key)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)

    raw = entry.get("upload_date") or entry.get("release_date")
    if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def normalize_entry(entry: dict[str, Any], kind: str, position: int) -> dict[str, Any] | None:
    video_id = str(entry.get("id") or "").strip()
    if not video_id:
        return None

    title = (entry.get("title") or f"Vídeo {video_id}").strip()
    dt = entry_datetime(entry)
    duration = entry.get("duration")
    if not isinstance(duration, (int, float)):
        duration = None

    return {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "kind": kind,
        "published": dt.isoformat() if dt else None,
        "date_source": "tab_approximate" if dt else None,
        "duration": duration,
        "channel": entry.get("channel") or entry.get("uploader") or "Maria Speaks English",
        "source_positions": {kind: position},
    }


def extract_tab(kind: str, url: str) -> list[dict[str, Any]]:
    # yt-dlp documents youtubetab:approximate_date for flat playlists. We keep
    # it because it is cheap and useful for ordering when YouTube exposes it,
    # but exact dates are subsequently cached from each video's watch page.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
        "extractor_args": {
            "youtubetab": {
                "approximate_date": [""],
            }
        },
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return []

    result: list[dict[str, Any]] = []
    for position, raw in enumerate(flatten_entries(info.get("entries") or []), start=1):
        item = normalize_entry(raw, kind, position)
        if item:
            result.append(item)
    return result


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def best_source_position(item: dict[str, Any]) -> int:
    positions = item.get("source_positions") or {}
    values = [v for v in positions.values() if isinstance(v, int) and v > 0]
    return min(values) if values else 10**9


def sort_key(item: dict[str, Any]) -> tuple[int, float, int, str]:
    dt = parse_iso(item.get("published"))
    if dt:
        return (1, dt.timestamp(), 0, item.get("id", ""))
    # For still-undated entries, preserve YouTube tab recency rather than
    # sorting by video id (which was the reason the first feed looked random).
    return (0, 0.0, -best_source_position(item), item.get("id", ""))


def merge_item(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = {**old, **new}

    positions = dict(old.get("source_positions") or {})
    positions.update(new.get("source_positions") or {})
    merged["source_positions"] = positions

    # An exact date obtained from the watch page must never be overwritten by
    # a missing or approximate flat-playlist date.
    if old.get("date_source") == "video_exact" and old.get("published"):
        merged["published"] = old["published"]
        merged["date_source"] = "video_exact"

    if old.get("duration") is not None and new.get("duration") is None:
        merged["duration"] = old["duration"]

    for key in ("date_checked_at", "date_check_failures"):
        if key in old and key not in merged:
            merged[key] = old[key]

    return merged


def update_catalog() -> list[dict[str, Any]]:
    previous_list = [item for item in load_json(CATALOG_PATH, []) if item.get("id")]
    previous = {item["id"]: item for item in previous_list}
    fresh: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for kind, url in TABS.items():
        try:
            for item in extract_tab(kind, url):
                old = fresh.get(item["id"]) or previous.get(item["id"], {})
                fresh[item["id"]] = merge_item(old, item)
        except Exception as exc:  # Keep the last known catalogue if YouTube blocks a tab.
            failures.append(f"{kind}: {exc}")

    if not fresh and previous:
        catalog = list(previous.values())
    else:
        merged = dict(previous)
        for video_id, item in fresh.items():
            merged[video_id] = merge_item(merged.get(video_id, {}), item)
        catalog = list(merged.values())

    catalog.sort(key=sort_key, reverse=True)

    if failures:
        print("Avisos de extracción:")
        for failure in failures:
            print(" -", failure)
    print(f"Catálogo descubierto: {len(catalog)} elementos únicos")
    return catalog


def should_retry_date(item: dict[str, Any], now: datetime) -> bool:
    if item.get("date_source") == "video_exact" and item.get("published"):
        return False
    checked = parse_iso(item.get("date_checked_at"))
    failures = int(item.get("date_check_failures") or 0)
    if checked and failures and now - checked < timedelta(hours=DATE_RETRY_HOURS):
        return False
    return True


def exact_metadata_from_info(info: dict[str, Any]) -> dict[str, Any]:
    dt = entry_datetime(info)
    result: dict[str, Any] = {}
    if dt:
        result["published"] = dt.isoformat()
        result["date_source"] = "video_exact"

    duration = info.get("duration")
    if isinstance(duration, (int, float)):
        result["duration"] = duration

    title = info.get("title")
    if isinstance(title, str) and title.strip():
        result["title"] = title.strip()

    channel = info.get("channel") or info.get("uploader")
    if isinstance(channel, str) and channel.strip():
        result["channel"] = channel.strip()

    thumbnail = info.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail.startswith("http"):
        result["thumbnail"] = thumbnail

    return result


def enrichment_priority(
    catalog: list[dict[str, Any]],
    preferred_ids: list[str],
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in catalog}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    # First correct whatever Feedly is currently seeing (recent + active batch).
    for video_id in preferred_ids:
        item = by_id.get(video_id)
        if item and video_id not in seen:
            ordered.append(item)
            seen.add(video_id)

    # Then work from the newest tab positions toward the oldest. This makes the
    # current end of the channel accurate before the historical tail.
    rest = [item for item in catalog if item["id"] not in seen]
    rest.sort(key=lambda x: (best_source_position(x), x.get("kind", ""), x.get("id", "")))
    ordered.extend(rest)
    return ordered


def enrich_exact_dates(catalog: list[dict[str, Any]], preferred_ids: list[str]) -> None:
    now = now_utc()
    candidates = [
        item
        for item in enrichment_priority(catalog, preferred_ids)
        if should_retry_date(item, now)
    ][:DATE_ENRICH_BATCH]

    if not candidates:
        exact_count = sum(1 for item in catalog if item.get("date_source") == "video_exact")
        print(f"Fechas exactas: {exact_count}/{len(catalog)} (sin pendientes elegibles en esta ejecución)")
        return

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 1,
    }

    success = 0
    failed = 0
    print(f"Enriqueciendo fechas reales: hasta {len(candidates)} vídeos...")

    with YoutubeDL(opts) as ydl:
        for index, item in enumerate(candidates, start=1):
            try:
                info = ydl.extract_info(item["url"], download=False)
                if not info:
                    raise RuntimeError("yt-dlp no devolvió metadatos")

                exact = exact_metadata_from_info(info)
                if exact.get("published"):
                    item.update(exact)
                    item["date_checked_at"] = now_utc().isoformat()
                    item["date_check_failures"] = 0
                    success += 1
                else:
                    item["date_checked_at"] = now_utc().isoformat()
                    item["date_check_failures"] = int(item.get("date_check_failures") or 0) + 1
                    failed += 1
            except Exception as exc:
                item["date_checked_at"] = now_utc().isoformat()
                item["date_check_failures"] = int(item.get("date_check_failures") or 0) + 1
                failed += 1
                print(f"  Aviso fecha {item['id']}: {exc}")

            if index % 15 == 0 or index == len(candidates):
                print(f"  Progreso fechas: {index}/{len(candidates)}; correctas {success}; fallos {failed}")
            if DATE_ENRICH_SLEEP > 0:
                time.sleep(DATE_ENRICH_SLEEP)

    exact_count = sum(1 for item in catalog if item.get("date_source") == "video_exact")
    print(f"Fechas exactas acumuladas: {exact_count}/{len(catalog)}")


def choose_feed_items(catalog: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state = load_json(
        STATE_PATH,
        {"done_ids": [], "active_ids": [], "active_since": None},
    )
    done = set(state.get("done_ids") or [])
    active = list(state.get("active_ids") or [])
    active_since = parse_iso(state.get("active_since"))
    now = now_utc()

    # Keep the current historical batch visible for at least 24 h so Feedly has
    # several polling opportunities before we rotate to the next batch.
    if active and active_since and now - active_since >= timedelta(hours=BACKFILL_HOLD_HOURS):
        done.update(active)
        active = []
        active_since = None

    recent = catalog[:RECENT_KEEP]
    recent_ids = {item["id"] for item in recent}
    done.update(recent_ids)

    by_id = {item["id"]: item for item in catalog}
    active = [video_id for video_id in active if video_id in by_id and video_id not in recent_ids]

    if not active:
        candidates = [
            item["id"]
            for item in catalog[RECENT_KEEP:]
            if item["id"] not in done
        ]
        active = candidates[:BACKFILL_BATCH]
        if active:
            active_since = now

    chosen_ids = list(dict.fromkeys([*(item["id"] for item in recent), *active]))
    chosen = [by_id[video_id] for video_id in chosen_ids if video_id in by_id]
    chosen.sort(key=sort_key, reverse=True)

    new_state = {
        "done_ids": sorted(done),
        "active_ids": active,
        "active_since": active_since.isoformat() if active_since else None,
        "catalog_count": len(catalog),
        "feed_count": len(chosen),
        "updated_at": now.isoformat(),
    }
    save_json(STATE_PATH, new_state)
    return chosen, new_state


def format_duration(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)):
        return None
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


def build_feed(items: list[dict[str, Any]], state: dict[str, Any]) -> None:
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    ET.register_namespace("media", "http://search.yahoo.com/mrss/")

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Maria Speaks English — YouTube completo"
    ET.SubElement(channel, "link").text = CHANNEL_URL
    ET.SubElement(channel, "description").text = (
        "Feed histórico y actualizado de Maria Speaks English: vídeos, Shorts y directos. "
        "Conserva la fecha original de publicación y usa relleno histórico rotatorio para Feedly."
    )
    ET.SubElement(channel, "language").text = "es"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now_utc())
    atom = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    atom.set("href", "feed.xml")
    atom.set("rel", "self")
    atom.set("type", "application/rss+xml")

    for data in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = data["title"]
        ET.SubElement(item, "link").text = data["url"]
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = f"youtube:{data['id']}"
        ET.SubElement(item, "category").text = data.get("kind") or "YouTube"

        published = parse_iso(data.get("published"))
        if published:
            ET.SubElement(item, "pubDate").text = format_datetime(published)

        duration = format_duration(data.get("duration"))
        parts = [f"<p><strong>{data.get('kind') or 'YouTube'}</strong></p>"]
        if published:
            precision = "fecha real" if data.get("date_source") == "video_exact" else "fecha aproximada"
            parts.append(
                f"<p>Publicado originalmente: {published.strftime('%d/%m/%Y')} ({precision})</p>"
            )
        if duration:
            parts.append(f"<p>Duración: {duration}</p>")
        parts.append(f"<p><a href=\"{data['url']}\">Abrir en YouTube</a></p>")
        ET.SubElement(item, "description").text = "".join(parts)

        thumb = ET.SubElement(item, "{http://search.yahoo.com/mrss/}thumbnail")
        thumb.set("url", data["thumbnail"])

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(FEED_PATH, encoding="utf-8", xml_declaration=True)
    print(
        f"Feed: {len(items)} entradas; lote histórico activo: "
        f"{len(state.get('active_ids') or [])}; total catálogo: {state.get('catalog_count', 0)}"
    )


def main() -> None:
    catalog = update_catalog()
    if not catalog:
        raise SystemExit("No se pudo obtener ningún elemento de YouTube y no existe catálogo previo.")

    # Select what Feedly should see, then give those IDs top priority for exact
    # date enrichment. The same GUID is preserved, so updating pubDate does not
    # create a second item.
    feed_items, state = choose_feed_items(catalog)
    preferred_ids = [item["id"] for item in feed_items]
    enrich_exact_dates(catalog, preferred_ids)

    # Exact dates can change the chronological order. Re-sort and rebuild the
    # chosen feed with the same IDs, then cache the improved catalogue.
    catalog.sort(key=sort_key, reverse=True)
    save_json(CATALOG_PATH, catalog)
    by_id = {item["id"]: item for item in catalog}
    refreshed_feed_items = [by_id[item["id"]] for item in feed_items if item["id"] in by_id]
    refreshed_feed_items.sort(key=sort_key, reverse=True)
    build_feed(refreshed_feed_items, state)


if __name__ == "__main__":
    main()
