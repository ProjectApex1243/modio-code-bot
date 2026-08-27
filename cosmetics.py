"""Cosmetic catalog used by the /lookup slash command.

cosmetics.json is generated from the Unity project (Assets/cosmetics_data.json)
and holds one object per item:
  {"id": "LHAAF.", "name": "BASEBALL CAP", "category": "Hat",
   "cost": 1000, "image": "LHAAF.png", "bundled": ["LBAAL.", ...]}

`image` names a file in cosmetic_images/ — the sprite cropped out of the game's
texture atlas. It is "" for the handful of items with no in-game icon.

The older compact list format (["LHAAF.", "BASEBALL CAP", [bundled ids]]) is
still accepted so an out-of-date catalog doesn't break the bot.
"""

import difflib
import json
from pathlib import Path

_CATALOG_PATH = Path(__file__).with_name("cosmetics.json")
IMAGE_DIR = Path(__file__).with_name("cosmetic_images")


def _normalize(entry) -> dict:
    if isinstance(entry, dict):
        return {
            "item_id": entry["id"],
            "display_name": (entry.get("name") or entry["id"]).strip(),
            "category": entry.get("category") or "",
            "cost": entry.get("cost") or 0,
            "image": entry.get("image") or "",
            "bundled_items": entry.get("bundled") or [],
        }
    # Legacy list form.
    item_id = entry[0]
    return {
        "item_id": item_id,
        "display_name": (entry[1] if len(entry) > 1 else item_id).strip(),
        "category": "",
        "cost": 0,
        "image": "",
        "bundled_items": entry[2] if len(entry) > 2 else [],
    }


def _load_catalog() -> list[dict]:
    with open(_CATALOG_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        item = _normalize(entry)
        if item["item_id"] in seen:
            continue
        seen.add(item["item_id"])
        items.append(item)
    return items


COSMETICS = _load_catalog()

_NAME_BY_ID = {item["item_id"]: item["display_name"] for item in COSMETICS}


def display_name_for(item_id: str) -> str | None:
    """Display name for an exact item id, or None if it isn't in the catalog."""
    return _NAME_BY_ID.get(item_id)


def image_path(item: dict) -> Path | None:
    """Local sprite file for an item, or None if it has no usable icon."""
    if not item.get("image"):
        return None
    path = IMAGE_DIR / item["image"]
    return path if path.is_file() else None


def search_cosmetics(query: str, limit: int = 8) -> list[dict]:
    """Ranked fuzzy search over display names (and raw item ids)."""
    q = query.strip().upper()
    if not q:
        return []

    scored: list[tuple[float, dict]] = []
    for item in COSMETICS:
        name = item["display_name"].upper()
        item_id = item["item_id"].upper()

        if q == name or q == item_id:
            score = 100.0
        elif name.startswith(q):
            score = 90.0
        elif q in name:
            score = 80.0
        elif q in item_id:
            score = 70.0
        else:
            # Similarity against the whole name plus the best single word,
            # so a typo like "bananna hat" still finds BANANA HAT.
            whole = difflib.SequenceMatcher(None, q, name).ratio()
            best_word = max(
                (difflib.SequenceMatcher(None, q, w).ratio() for w in name.split()),
                default=0.0,
            )
            score = max(whole, best_word) * 60.0
            if score < 33.0:
                continue
        scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], pair[1]["display_name"]))
    return [item for _score, item in scored[:limit]]
