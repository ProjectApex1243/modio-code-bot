"""Staff admin operations against Supabase: bans, ban perms, cosmetic grants,
leaderboard resets, and the redemption-code kill switch.

Each helper wraps one of the SQL snippets staff used to run by hand in the
Supabase SQL editor. The tables involved are RLS-locked with no policies, so
every call here needs the SERVICE ROLE key (bot/server only — never the game
client).
"""

import json
import math
import re
from datetime import timedelta
from typing import Any

BAN_PERMISSIONS_TABLE = "ban_permissions"
BANNED_PLAYERS_TABLE = "banned_players"
INVENTORY_TABLE = "user_inventory"
LEADERBOARD_TABLE = "tag_leaderboard"
TITLE_DATA_TABLE = "title_data"
CODES_TABLE = "redemption_codes"
TICKET_CLAIMS_TABLE = "discord_ticket_claims"

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


def duration_to_hours(duration: timedelta | None) -> int:
    """Converts a ban length into the whole hours the ban_player RPC expects.

    The RPC treats <= 0 as permanent, so a sub-hour ban is rounded UP to one
    hour rather than being silently turned into a permanent one.
    """
    if duration is None:
        return 0
    return max(1, math.ceil(duration.total_seconds() / 3600))


async def ban_player(
    session, supabase_url, key,
    user_id: str,
    reason: str,
    duration: timedelta | None,
) -> dict | None:
    """Bans a player through the `ban_player` RPC, the same path staff use.

    The RPC upserts, so re-banning someone updates their existing ban instead
    of failing, and it records who issued the ban. Returns the resulting
    banned_players row.
    """
    await _rest(
        session, "POST", supabase_url, key, "rpc/ban_player",
        json_body={
            "target_user_id": user_id,
            "ban_reason": reason,
            "ban_duration_hours": duration_to_hours(duration),
        },
    )
    return await fetch_ban(session, supabase_url, key, user_id)


async def fetch_ban(session, supabase_url, key, user_id: str) -> dict | None:
    """The player's banned_players row, or None if they aren't banned."""
    rows = await _rest(
        session, "GET", supabase_url, key, BANNED_PLAYERS_TABLE,
        params={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"},
    ) or []
    return rows[0] if rows else None


async def unban_player(session, supabase_url, key, user_id: str) -> bool:
    """Unbans a player through the `unban_player` RPC, the same path staff use.

    Returns False if they weren't banned in the first place — the RPC reports
    success either way, so that's checked up front.
    """
    if await fetch_ban(session, supabase_url, key, user_id) is None:
        return False
    await _rest(
        session, "POST", supabase_url, key, "rpc/unban_player",
        json_body={"target_user_id": user_id},
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


async def fetch_ticket_claim(session, supabase_url, key, discord_id: str) -> dict | None:
    """The user's cosmetics-ticket claim row, or None if they haven't claimed."""
    rows = await _rest(
        session, "GET", supabase_url, key, TICKET_CLAIMS_TABLE,
        params={"select": "*", "discord_id": f"eq.{discord_id}", "limit": "1"},
    ) or []
    return rows[0] if rows else None


async def stake_ticket_claim(
    session, supabase_url, key, discord_id: str, discord_tag: str
) -> bool:
    """Reserves the one-per-user cosmetics claim, before any code is minted.

    discord_id is the table's primary key, so two rapid clicks can't both get
    through — the loser gets a duplicate-key 409 and is told they already
    claimed. Returns False in that case.
    """
    try:
        await _rest(
            session, "POST", supabase_url, key, TICKET_CLAIMS_TABLE,
            json_body={"discord_id": discord_id, "discord_tag": discord_tag},
        )
        return True
    except SupaError as error:
        if error.status == 409:
            return False
        raise


async def attach_ticket_claim_code(
    session, supabase_url, key, discord_id: str, code: str
) -> None:
    """Records which code the claim handed out, so it can be shown again later."""
    await _rest(
        session, "PATCH", supabase_url, key, TICKET_CLAIMS_TABLE,
        params={"discord_id": f"eq.{discord_id}"}, json_body={"code": code},
    )


async def release_ticket_claim(session, supabase_url, key, discord_id: str) -> None:
    """Undoes a staked claim when minting the code failed, so the user isn't
    locked out of a reward they never received."""
    await _rest(
        session, "DELETE", supabase_url, key, TICKET_CLAIMS_TABLE,
        params={"discord_id": f"eq.{discord_id}"},
    )


async def disable_code(session, supabase_url, key, code: str) -> bool:
    """UPDATE redemption_codes SET enabled = false — kills a code instantly.
    Returns False if no code by that name exists."""
    rows = await _rest(
        session, "PATCH", supabase_url, key, CODES_TABLE,
        params={"code": f"eq.{code}"},
        json_body={"enabled": False}, representation=True,
    )
    return bool(rows)
