"""Staff admin operations against Supabase: bans, ban perms, cosmetic grants,
leaderboard resets, and the redemption-code kill switch.

Each helper wraps one of the SQL snippets staff used to run by hand in the
Supabase SQL editor. The tables involved are RLS-locked with no policies, so
every call here needs the SERVICE ROLE key (bot/server only — never the game
client).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

BAN_PERMISSIONS_TABLE = "ban_permissions"
BANNED_PLAYERS_TABLE = "banned_players"
INVENTORY_TABLE = "user_inventory"
LEADERBOARD_TABLE = "tag_leaderboard"
TITLE_DATA_TABLE = "title_data"
CODES_TABLE = "redemption_codes"

# Reads are capped so one command never tries to render an unbounded table.
LIST_FETCH_LIMIT = 200
# A whole inventory isn't rendered as-is, so this only has to clear the catalog
# size (~1900 items) for /give-all-cosmetics to see everything a player owns.
INVENTORY_FETCH_LIMIT = 10000

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# The catalog stored in title_data has used a few spellings for the item id.
CATALOG_ID_KEYS = ("item_id", "id", "itemId", "ItemId")


class SupaError(RuntimeError):
    """A Supabase REST call came back non-2xx. `.status` carries the HTTP code
    so callers can special-case conflicts (409 = row already exists)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Supabase request failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def validate_user_id(raw: str) -> str:
    """Normalizes and validates a player UUID typed by staff; raises ValueError
    so the command can answer with something friendlier than a Postgres 22P02."""
    cleaned = raw.strip().strip("{}").lower()
    if not _UUID_RE.match(cleaned):
        raise ValueError(
            f"`{raw.strip()}` doesn't look like a player UUID. Expected the form "
            "`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`."
        )
    return cleaned


async def _rest(
    session: "aiohttp.ClientSession",
    method: str,
    supabase_url: str,
    service_role_key: str,
    table: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    representation: bool = False,
    ignore_duplicates: bool = False,
) -> Any:
    """One PostgREST call. Returns the parsed JSON body (None on 204)."""
    endpoint = f"{supabase_url}/rest/v1/{table}"
    headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    prefer = []
    if representation:
        prefer.append("return=representation")
    if ignore_duplicates:
        # PostgREST's ON CONFLICT DO NOTHING. Valid with or without a unique
        # constraint on the table, so this is safe either way.
        prefer.append("resolution=ignore-duplicates")
    if prefer:
        headers["Prefer"] = ",".join(prefer)

    async with session.request(
        method, endpoint, headers=headers, params=params, json=json_body
    ) as response:
        if response.status not in (200, 201, 204):
            raise SupaError(response.status, await response.text())
        # Without Prefer: return=representation, PostgREST answers with an empty
        # body, which json() would choke on — treat that as "no rows returned".
        body = (await response.text()).strip()
        if not body:
            return None
        return json.loads(body)


def _in_filter(values: list[str]) -> str:
    """PostgREST `in.(...)` filter; item ids can contain spaces so every value
    is quoted, with quotes/backslashes escaped."""
    quoted = ",".join(
        '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values
    )
    return f"in.({quoted})"


# --- Ban permissions ---------------------------------------------------------


async def give_ban_perms(session, supabase_url, key, user_id: str) -> bool:
    """INSERT INTO ban_permissions. Returns False if they already had perms."""
    try:
        await _rest(
            session, "POST", supabase_url, key, BAN_PERMISSIONS_TABLE,
            json_body={"user_id": user_id}, representation=True,
        )
        return True
    except SupaError as error:
        if error.status == 409:
            return False
        raise


async def remove_ban_perms(session, supabase_url, key, user_id: str) -> bool:
    """DELETE FROM ban_permissions. Returns False if they had no perms row."""
    rows = await _rest(
        session, "DELETE", supabase_url, key, BAN_PERMISSIONS_TABLE,
        params={"user_id": f"eq.{user_id}"}, representation=True,
    )
    return bool(rows)


async def list_ban_perms(session, supabase_url, key) -> list[dict]:
    return await _rest(
        session, "GET", supabase_url, key, BAN_PERMISSIONS_TABLE,
        params={"select": "*", "limit": str(LIST_FETCH_LIMIT)},
    ) or []


# --- Bans --------------------------------------------------------------------


async def ban_player(
    session, supabase_url, key,
    user_id: str,
    reason: str,
    duration: timedelta | None,
) -> dict:
    """INSERT INTO banned_players — permanent when duration is None, otherwise
    banned_until = now() + duration. Raises SupaError(409) if already banned."""
    body: dict[str, Any] = {
        "user_id": user_id,
        "reason": reason,
        "is_permanent": duration is None,
    }
    if duration is not None:
        body["banned_until"] = (datetime.now(timezone.utc) + duration).isoformat()
    rows = await _rest(
        session, "POST", supabase_url, key, BANNED_PLAYERS_TABLE,
        json_body=body, representation=True,
    )
    return rows[0] if isinstance(rows, list) and rows else body


async def is_banned(session, supabase_url, key, user_id: str) -> bool:
    rows = await _rest(
        session, "GET", supabase_url, key, BANNED_PLAYERS_TABLE,
        params={"select": "user_id", "user_id": f"eq.{user_id}", "limit": "1"},
    ) or []
    return bool(rows)


async def unban_player(session, supabase_url, key, user_id: str) -> bool:
    """Unbans a player. Returns False if they weren't banned in the first place.

    Prefers the `unban_player` RPC, since that's the path staff use in the SQL
    editor and it may do more than clear the row (audit trail, related state).
    PostgREST needs the argument by name and the function's parameter name isn't
    recorded anywhere in this repo, so the likely spellings are tried in turn;
    if none of them resolve, this falls back to deleting the row directly.
    """
    if not await is_banned(session, supabase_url, key, user_id):
        return False

    for param in ("p_user_id", "user_id", "p_uuid", "target_user_id"):
        try:
            await _rest(
                session, "POST", supabase_url, key, "rpc/unban_player",
                json_body={param: user_id},
            )
            return True
        except SupaError as error:
            # 404 = no such function, 400 = wrong argument name for it.
            if error.status not in (400, 404):
                raise

    await _rest(
        session, "DELETE", supabase_url, key, BANNED_PLAYERS_TABLE,
        params={"user_id": f"eq.{user_id}"},
    )
    return True


async def list_banned(session, supabase_url, key) -> list[dict]:
    return await _rest(
        session, "GET", supabase_url, key, BANNED_PLAYERS_TABLE,
        params={"select": "*", "limit": str(LIST_FETCH_LIMIT)},
    ) or []


# --- Inventory / cosmetics ---------------------------------------------------


async def fetch_inventory(session, supabase_url, key, user_id: str) -> list[str]:
    """The player's item ids, deduplicated, in the order Supabase returns them.

    The limit is well above the catalog size so a player who owns everything
    still comes back in full.
    """
    rows = await _rest(
        session, "GET", supabase_url, key, INVENTORY_TABLE,
        params={
            "select": "item_id",
            "user_id": f"eq.{user_id}",
            "limit": str(INVENTORY_FETCH_LIMIT),
        },
    ) or []
    seen: set[str] = set()
    item_ids: list[str] = []
    for row in rows:
        item_id = str(row.get("item_id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            item_ids.append(item_id)
    return item_ids


async def grant_items(
    session, supabase_url, key, user_id: str, item_ids: list[str]
) -> tuple[list[str], list[str]]:
    """INSERT INTO user_inventory for every item the player doesn't already
    own (the Python version of ON CONFLICT DO NOTHING, so it works whether or
    not the table has a unique constraint). Returns (granted, already_owned)."""
    owned = set(await fetch_inventory(session, supabase_url, key, user_id))
    granted = [item_id for item_id in item_ids if item_id not in owned]
    already = [item_id for item_id in item_ids if item_id in owned]
    if granted:
        await _rest(
            session, "POST", supabase_url, key, INVENTORY_TABLE,
            json_body=[{"user_id": user_id, "item_id": item_id} for item_id in granted],
            ignore_duplicates=True,
        )
    return granted, already


async def remove_items(
    session, supabase_url, key, user_id: str, item_ids: list[str]
) -> tuple[list[str], list[str]]:
    """DELETE FROM user_inventory for the given items.
    Returns (removed, not_owned)."""
    rows = await _rest(
        session, "DELETE", supabase_url, key, INVENTORY_TABLE,
        params={"user_id": f"eq.{user_id}", "item_id": _in_filter(item_ids)},
        representation=True,
    ) or []
    removed_set = {str(row.get("item_id")) for row in rows}
    removed = [item_id for item_id in item_ids if item_id in removed_set]
    not_owned = [item_id for item_id in item_ids if item_id not in removed_set]
    return removed, not_owned


async def clear_inventory(session, supabase_url, key, user_id: str) -> int:
    """DELETE FROM user_inventory WHERE user_id — the whole inventory. Returns
    how many rows were deleted."""
    rows = await _rest(
        session, "DELETE", supabase_url, key, INVENTORY_TABLE,
        params={"user_id": f"eq.{user_id}"}, representation=True,
    ) or []
    return len(rows)


async def fetch_catalog_item_ids(session, supabase_url, key) -> list[str]:
    """Item ids from the game catalog stored in title_data (key = 'catalog') —
    the source the GIVE EVERY COSMETIC query reads. The value column may hold
    JSON text or already-parsed JSON depending on the column type."""
    rows = await _rest(
        session, "GET", supabase_url, key, TITLE_DATA_TABLE,
        params={"select": "value", "key": "eq.catalog", "limit": "1"},
    ) or []
    if not rows:
        return []

    value = rows[0].get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []

    seen: set[str] = set()
    item_ids: list[str] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        for id_key in CATALOG_ID_KEYS:
            item_id = entry.get(id_key)
            if item_id:
                item_id = str(item_id)
                if item_id not in seen:
                    seen.add(item_id)
                    item_ids.append(item_id)
                break
    return item_ids


# --- Leaderboard -------------------------------------------------------------


async def reset_score(session, supabase_url, key, user_id: str) -> bool:
    """UPDATE tag_leaderboard SET total_tags = 0. Returns False if the player
    has no leaderboard row."""
    rows = await _rest(
        session, "PATCH", supabase_url, key, LEADERBOARD_TABLE,
        params={"player_id": f"eq.{user_id}"},
        json_body={"total_tags": 0}, representation=True,
    )
    return bool(rows)


async def fetch_leaderboard(session, supabase_url, key, limit: int) -> list[dict]:
    """SELECT * FROM tag_leaderboard ORDER BY total_tags DESC LIMIT n."""
    return await _rest(
        session, "GET", supabase_url, key, LEADERBOARD_TABLE,
        params={"select": "*", "order": "total_tags.desc", "limit": str(limit)},
    ) or []


# --- Redemption codes --------------------------------------------------------


async def disable_code(session, supabase_url, key, code: str) -> bool:
    """UPDATE redemption_codes SET enabled = false — kills a code instantly.
    Returns False if no code by that name exists."""
    rows = await _rest(
        session, "PATCH", supabase_url, key, CODES_TABLE,
        params={"code": f"eq.{code}"},
        json_body={"enabled": False}, representation=True,
    )
    return bool(rows)
