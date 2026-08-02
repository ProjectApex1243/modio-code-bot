"""Regenerate cosmetics.json and cosmetic_images/ from the Unity project.

Run this after cosmetics are added or reworked in game:

    pip install pillow
    python tools/extract_cosmetics.py "P:/Project Apex V3/ExportedProject"

Unity's exported sprites are atlas regions: Sprite/<name>.asset holds an m_Rect
plus a GUID pointing at the packed atlas PNG under Texture2D/. We crop each
region out and write a standalone PNG named after the cosmetic's item ID.

Set contents (which item IDs a bundle grants) only exist in this repo's
cosmetics.json, not in the Unity export, so they are carried over from whatever
catalog is already checked in.
"""

import json
import re
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO / "cosmetics.json"
IMAGE_DIR = REPO / "cosmetic_images"

_GUID_RE = re.compile(r"^guid:\s*([0-9a-f]{32})", re.M)
_SUFFIX_RE = re.compile(r"_\d+$")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga"}


def texture_guid_map(assets: Path) -> dict[str, Path]:
    """guid -> texture file, for every image .meta in the project."""
    mapping: dict[str, Path] = {}
    for meta in assets.rglob("*.meta"):
        asset = meta.with_suffix("")
        if asset.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        match = _GUID_RE.search(meta.read_text(encoding="utf-8", errors="ignore"))
        if match:
            mapping[match.group(1)] = asset
    return mapping


def parse_sprite(path: Path) -> dict | None:
    """Pull name, atlas rect and atlas GUID out of a Sprite .asset."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    rect = re.search(
        r"m_Rect:\s*\n\s*serializedVersion:\s*\d+\s*\n"
        r"\s*x:\s*(-?[\d.]+)\s*\n\s*y:\s*(-?[\d.]+)\s*\n"
        r"\s*width:\s*(-?[\d.]+)\s*\n\s*height:\s*(-?[\d.]+)",
        text,
    )
    tex = re.search(r"texture:\s*\{fileID:\s*\d+,\s*guid:\s*([0-9a-f]{32})", text)
    name = re.search(r"^  m_Name:\s*(.+)$", text, re.M)
    if not (rect and tex and name):
        return None
    return {
        "name": name.group(1).strip(),
        "rect": tuple(float(rect.group(i)) for i in range(1, 5)),
        "guid": tex.group(1),
    }


def build_sprite_index(assets: Path) -> dict[str, dict]:
    """Lookup keyed by lowercase sprite name, with `_0`/`_1` duplicates as fallbacks.

    AssetRipper emits the same sprite several times (`aviators`, `aviators_0`, …)
    and sometimes only under a suffixed or differently-cased name, so index both
    the full name and the de-suffixed base name. Unsuffixed spellings win.
    """
    index: dict[str, dict] = {}
    for path in sorted((assets / "Sprite").glob("*.asset")):
        sprite = parse_sprite(path)
        if not sprite or sprite["rect"][2] < 1 or sprite["rect"][3] < 1:
            continue
        full = sprite["name"].lower()
        index.setdefault(full, sprite)
        index.setdefault(_SUFFIX_RE.sub("", full), sprite)
    return index


def crop(sprite: dict, textures: dict[str, Path], cache: dict[str, Image.Image]):
    tex_path = textures.get(sprite["guid"])
    if not tex_path or not tex_path.exists():
        return None
    key = str(tex_path)
    if key not in cache:
        cache[key] = Image.open(tex_path).convert("RGBA")
    atlas = cache[key]
    x, y, w, h = (int(round(v)) for v in sprite["rect"])
    # Unity rects are bottom-left origin; PIL's box is top-left.
    top = atlas.height - y - h
    if x < 0 or top < 0 or x + w > atlas.width or top + h > atlas.height:
        return None
    return atlas.crop((x, top, x + w, top + h))


def safe_stem(item_id: str) -> str:
    """Item IDs end in a '.', which Windows strips from filenames."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", item_id.rstrip("."))


def existing_entries() -> dict[str, dict]:
    """Checked-in catalog keyed by item id, so custom items survive a rebuild."""
    if not CATALOG_PATH.is_file():
        return {}
    entries: dict[str, dict] = {}
    for entry in json.loads(CATALOG_PATH.read_text(encoding="utf-8")):
        if isinstance(entry, dict):
            entries[entry["id"]] = entry
            continue
        item = {
            "id": entry[0],
            "name": entry[1] if len(entry) > 1 else entry[0],
            "category": "",
            "cost": 0,
            "image": "",
        }
        if len(entry) > 2:
            item["bundled"] = entry[2]
        entries[entry[0]] = item
    return entries


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    project = Path(argv[1])
    assets = project / "Assets" if (project / "Assets").is_dir() else project
    source = assets / "cosmetics_data.json"
    if not source.is_file():
        print(f"cosmetics_data.json not found under {assets}")
        return 1

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    previous = existing_entries()
    bundles = {k: v["bundled"] for k, v in previous.items() if v.get("bundled")}

    print("indexing textures…")
    textures = texture_guid_map(assets)
    print(f"  {len(textures)} textures")
    print("indexing sprites…")
    sprites = build_sprite_index(assets)
    print(f"  {len(sprites)} sprite names")

    cache: dict[str, Image.Image] = {}
    catalog: list[dict] = []
    seen: set[str] = set()
    no_picture = missing_sprite = failed = 0

    for item in json.loads(source.read_text(encoding="utf-8"))["cosmetics"]:
        item_id = item["itemName"]
        if item_id in seen:
            continue
        seen.add(item_id)

        image_name = ""
        picture = (item.get("itemPicture") or "").strip()
        if not picture:
            no_picture += 1
        else:
            sprite = sprites.get(picture.lower()) or sprites.get(
                _SUFFIX_RE.sub("", picture.lower())
            )
            if not sprite:
                missing_sprite += 1
            else:
                cropped = crop(sprite, textures, cache)
                if cropped is None:
                    failed += 1
                else:
                    image_name = f"{safe_stem(item_id)}.png"
                    cropped.save(IMAGE_DIR / image_name)

        entry = {
            "id": item_id,
            "name": (item.get("displayName") or item_id).strip(),
            "category": item.get("category") or "",
            "cost": item.get("cost", 0),
            "image": image_name,
        }
        if bundles.get(item_id):
            entry["bundled"] = bundles[item_id]
        catalog.append(entry)

    # Custom items the Unity export doesn't cover keep their checked-in entry.
    for item_id, entry in previous.items():
        if item_id not in seen:
            catalog.append(entry)

    CATALOG_PATH.write_text(json.dumps(catalog, indent=0, ensure_ascii=False), encoding="utf-8")

    with_image = sum(1 for c in catalog if c["image"])
    print(f"\ncatalog: {len(catalog)} items, {with_image} with images -> {CATALOG_PATH.name}")
    print(
        f"no itemPicture: {no_picture} | sprite not found: {missing_sprite} "
        f"| crop failed: {failed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
