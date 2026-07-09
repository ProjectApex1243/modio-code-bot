"""Cosmetic name/ID lookup used by the /lookup slash command.

The catalog lives in cosmetics.json as a list of compact entries:
  ["LHAAA.", "BANANA HAT"]                     id + display name
  ["LBASG."]                                   unnamed item (name == id)
  ["LSAAA.", "CLOWN SET", ["LBAAL.", ...]]     optional bundled item ids
"""

import difflib
import json
from pathlib import Path

_CATALOG_PATH = Path(__file__).with_name("cosmetics.json")


def _load_catalog() -> list[dict]:
    with open(_CATALOG_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    items: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        item_id = entry[0]
        if item_id in seen:
            continue
        seen.add(item_id)
        display_name = entry[1] if len(entry) > 1 else item_id
        items.append(
            {
                "item_id": item_id,
                "display_name": display_name.strip(),
                "bundled_items": entry[2] if len(entry) > 2 else [],
            }
        )
    return items


COSMETICS = _load_catalog()


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
