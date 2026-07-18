"""Creates redemption codes in the Supabase `redemption_codes` table.

The in-game computer's REDEEM tab calls the `redeem_code` RPC, which reads this
table. The table has RLS enabled with no policies, so inserts must use the
SERVICE ROLE key (never ship that key in the game client — bot/server only).
"""

import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from cosmetics import COSMETICS

# The in-game keyboard caps the code field at exactly 8 characters (A-Z / 0-9).
CODE_LENGTH = 8
CODE_ALPHABET = string.ascii_uppercase + string.digits

_DURATION_PART = re.compile(r"(\d+)\s*([mhdw])", re.IGNORECASE)
_DURATION_UNITS = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}

# item_id / display_name (upper-cased) -> canonical item_id, for resolving what
# staff type into the exact ids the game's user_inventory expects.
_ID_LOOKUP: dict[str, str] = {}
for _item in COSMETICS:
    _ID_LOOKUP.setdefault(_item["item_id"].upper(), _item["item_id"])
    _ID_LOOKUP.setdefault(_item["display_name"].upper(), _item["item_id"])


def generate_code() -> str:
    """Random 8-char code using only characters the in-game keyboard can type."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def validate_code(code: str) -> str:
    """Uppercases and validates a staff-supplied code; raises ValueError if unusable."""
    cleaned = code.strip().upper()
    if len(cleaned) != CODE_LENGTH:
        raise ValueError(
            f"Code must be exactly {CODE_LENGTH} characters (the in-game keyboard "
            f"requires it) — got {len(cleaned)}."
        )
    if any(ch not in CODE_ALPHABET for ch in cleaned):
        raise ValueError("Code can only contain letters A-Z and digits 0-9.")
    return cleaned


def parse_duration(text: str) -> timedelta:
    """Parses '30m', '1h', '2d', '1w', or combos like '1d12h' into a timedelta."""
    parts = _DURATION_PART.findall(text.strip())
    if not parts or _DURATION_PART.sub("", text.strip()).strip():
        raise ValueError(
            "Could not understand that duration. Use forms like `30m`, `1h`, `2d`, "
            "`1w`, or combos like `1d12h`."
        )
    total = timedelta()
    for amount, unit in parts:
        total += int(amount) * _DURATION_UNITS[unit.lower()]
    if total <= timedelta():
        raise ValueError("Duration must be greater than zero.")
    return total


def resolve_items(raw: str) -> tuple[list[str], list[str]]:
    """Splits a comma-separated list of item ids/names into canonical item ids.

    Item ids may contain spaces (e.g. "Discord Badge"), so commas are the only
    separator. Returns (item_ids, unknown_tokens); unknown tokens are still
    included in item_ids as typed, since custom cosmetics may be missing from
    cosmetics.json — the caller should surface them as a warning.
    """
    item_ids: list[str] = []
    unknown: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        resolved = _ID_LOOKUP.get(token.upper())
        if resolved is None:
            unknown.append(token)
            item_ids.append(token)
        elif resolved not in item_ids:
            item_ids.append(resolved)
    if not item_ids:
        raise ValueError("No cosmetic items given. Separate multiple items with commas.")
    return item_ids, unknown


async def create_code(
    session: "aiohttp.ClientSession",
    supabase_url: str,
    service_role_key: str,
    code: str,
    items: list[str],
    max_uses: int | None,
    expires_in: timedelta | None,
) -> dict[str, Any]:
    """Inserts one row into redemption_codes. Returns the created row."""
    expires_at = (
        (datetime.now(timezone.utc) + expires_in).isoformat() if expires_in else None
    )
    endpoint = f"{supabase_url}/rest/v1/redemption_codes"
    headers = {
        "Content-Type": "application/json",
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Prefer": "return=representation",
    }
    body = {
        "code": code,
        "items": items,
        "max_uses": max_uses,
        "expires_at": expires_at,
    }

    async with session.post(endpoint, headers=headers, json=body) as response:
        if response.status in (200, 201):
            rows = await response.json()
            return rows[0] if isinstance(rows, list) and rows else body
        error_text = await response.text()
        if response.status == 409:
            raise RuntimeError(f"Code `{code}` already exists — pick a different one.")
        raise RuntimeError(f"Supabase insert failed ({response.status}): {error_text}")
