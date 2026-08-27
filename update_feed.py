#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from yt_dlp import YoutubeDL

CHANNEL_HANDLE = os.getenv("CHANNEL_HANDLE", "mariaspeaksenglish").lstrip("@")
CHANNEL_URL = f"https://www.youtube.com/@{CHANNEL_HANDLE}"
TABS = {
    "Canal": CHANNEL_URL,
    "Vídeo": f"{CHANNEL_URL}/videos",
    "Short": f"{CHANNEL_URL}/shorts",
    "Directo": f"{CHANNEL_URL}/streams",
}

RECENT_KEEP = int(os.getenv("RECENT_KEEP", "75"))
BACKFILL_BATCH = int(os.getenv("BACKFILL_BATCH", "100"))
BACKFILL_HOLD_HOURS = int(os.getenv("BACKFILL_HOLD_HOURS", "24"))

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


def normalize_entry(entry: dict[str, Any], kind: str) -> dict[str, Any] | None:
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
        "duration": duration,
        "channel": entry.get("channel") or entry.get("uploader") or "Maria Speaks English",
    }


def extract_tab(kind: str, url: str) -> list[dict[str, Any]]:
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
    for raw in flatten_entries(info.get("entries") or []):
        item = normalize_entry(raw, kind)
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


def sort_key(item: dict[str, Any]) -> tuple[datetime, str]:
    dt = parse_iso(item.get("published")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (dt, item.get("id", ""))


def update_catalog() -> list[dict[str, Any]]:
    previous = {item["id"]: item for item in load_json(CATALOG_PATH, []) if item.get("id")}
    fresh: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for kind, url in TABS.items():
        try:
            for item in extract_tab(kind, url):
                old = previous.get(item["id"], {})
                # Preserve an older exact/approximate date if a later extraction omits it.
                if not item.get("published") and old.get("published"):
                    item["published"] = old["published"]
                fresh[item["id"]] = {**old, **item}
        except Exception as exc:  # Keep the last known catalogue if YouTube blocks a tab.
            failures.append(f"{kind}: {exc}")

    if not fresh and previous:
        catalog = list(previous.values())
    else:
        # Preserve previously discovered historical items even if a tab is temporarily incomplete.
        merged = dict(previous)
        merged.update(fresh)
        catalog = list(merged.values())

    catalog.sort(key=sort_key, reverse=True)
    save_json(CATALOG_PATH, catalog)

    if failures:
        print("Avisos de extracción:")
        for failure in failures:
            print(" -", failure)
    print(f"Catálogo: {len(catalog)} elementos únicos")
    return catalog


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
        "Incluye relleno histórico rotatorio para facilitar que Feedly indexe el catálogo antiguo."
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
            parts.append(f"<p>Publicado originalmente: {published.strftime('%d/%m/%Y')}</p>")
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
    feed_items, state = choose_feed_items(catalog)
    build_feed(feed_items, state)


if __name__ == "__main__":
    main()
