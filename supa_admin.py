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
from datetime import datetime, timedelta, timezone
from typing import Any

# Same table supabase_rooms reads for /find-active-rooms; imported rather than
# re-spelled so a rename only has to happen in one place.
from supabase_rooms import PRESENCE_TABLE

BAN_PERMISSIONS_TABLE = "ban_permissions"
BANNED_PLAYERS_TABLE = "banned_players"
INVENTORY_TABLE = "user_inventory"
LEADERBOARD_TABLE = "tag_leaderboard"
TITLE_DATA_TABLE = "title_data"
CODES_TABLE = "redemption_codes"
TICKET_CLAIMS_TABLE = "discord_ticket_claims"
# The account system's own record of a player's name; one row per user_id.
PROFILES_TABLE = "player_profiles"

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


async def transfer_cosmetics(
    session, supabase_url, key, *, old_user_id: str, new_user_id: str
) -> tuple[list[str], list[str]]:
    """Moves every cosmetic old_user_id owns onto new_user_id.

    Granted to the new account FIRST, removed from the old account second: if
    a request fails partway through, the player ends up owning items on BOTH
    accounts rather than neither. Returns (granted, already_on_new) - the
    items new_user_id didn't already have, and the ones it did (those are
    still removed from the old account, since the new one keeps them either
    way).
    """
    item_ids = await fetch_inventory(session, supabase_url, key, old_user_id)
    if not item_ids:
        return [], []
    granted, already_on_new = await grant_items(
        session, supabase_url, key, new_user_id, item_ids
    )
    await remove_items(session, supabase_url, key, old_user_id, item_ids)
    return granted, already_on_new


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


# --- Player identity ---------------------------------------------------------


async def fetch_profile(session, supabase_url, key, user_id: str) -> dict | None:
    """SELECT * FROM player_profiles WHERE user_id = ... — the account's current
    display name. None if the game has never written a profile for this UUID."""
    rows = await _rest(
        session, "GET", supabase_url, key, PROFILES_TABLE,
        params={"select": "*", "user_id": f"eq.{user_id}", "limit": "1"},
    ) or []
    return rows[0] if rows else None


async def fetch_presence_row(session, supabase_url, key, user_id: str) -> dict | None:
    """The player's friendpresence row: the name the game client last reported and
    where it reported from.

    Keyed on playfabid, which holds the same UUID as player_profiles.user_id. The
    columns are `select: *` because this table is written by the game client and
    its column names have drifted between versions.
    """
    rows = await _rest(
        session, "GET", supabase_url, key, PRESENCE_TABLE,
        params={"select": "*", "playfabid": f"eq.{user_id}", "limit": "1"},
    ) or []
    return rows[0] if rows else None


async def fetch_profile_names(
    session, supabase_url, key, user_ids: list[str]
) -> dict[str, str]:
    """{user_id: display_name} for a batch of players, in one request.

    Ids with no profile row are simply absent from the result, so callers should
    fall back to showing the raw id.
    """
    if not user_ids:
        return {}
    rows = await _rest(
        session, "GET", supabase_url, key, PROFILES_TABLE,
        params={
            "select": "user_id,display_name",
            "user_id": _in_filter(user_ids),
            "limit": str(len(user_ids)),
        },
    ) or []
    return {
        str(row["user_id"]): str(row.get("display_name") or "")
        for row in rows
        if row.get("user_id")
    }


# --- Ban attribution ---------------------------------------------------------

# banned_players is read a page at a time: PostgREST caps how many rows one
# response will carry (measured at 1000 on this project) and silently truncates
# past it, so asking for the whole table in one request would undercount.
BAN_ISSUER_PAGE = 1000
# Backstop against an unbounded loop if the ban table ever grows past what this
# command should be scanning in one go.
BAN_ISSUER_MAX_ROWS = 100000


async def fetch_ban_counts_by_issuer(
    session, supabase_url, key
) -> tuple[dict[str, int], int]:
    """SELECT banned_by, count(1) FROM banned_players GROUP BY banned_by.

    PostgREST has no GROUP BY, so the ids are paged in and tallied here. Only that
    one column is selected, so the payload stays small even on a big ban table.

    Returns the tally alongside the number of bans with no issuer recorded — older
    rows and anything banned automatically rather than by a person.
    """
    counts: dict[str, int] = {}
    unattributed = 0
    offset = 0
    while offset < BAN_ISSUER_MAX_ROWS:
        rows = await _rest(
            session, "GET", supabase_url, key, BANNED_PLAYERS_TABLE,
            params={
                "select": "banned_by",
                # A stable sort, so paging can't skip or double-count a row the
                # way it could over an unordered result.
                "order": "id",
                "limit": str(BAN_ISSUER_PAGE),
                "offset": str(offset),
            },
        ) or []
        # Advance by what actually came back rather than by the page size: the
        # server is free to return fewer rows than asked for, and treating a
        # capped page as the last one would drop every ban after it.
        if not rows:
            break
        for row in rows:
            issuer = row.get("banned_by")
            if issuer:
                counts[str(issuer)] = counts.get(str(issuer), 0) + 1
            else:
                unattributed += 1
        offset += len(rows)
    return counts, unattributed


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


# --- Account recovery (App Lab move) -----------------------------------------
#
# Meta user ids are APP-SCOPED, so the same person gets a different id under a
# new App ID and the new build cannot find their account. Nothing is lost: every
# table keys on the auth uuid, not the Meta id. Recovery moves no data, it just
# repoints the new Meta id at the uuid that already exists:
#
#   1. the throwaway account from the new build's first login releases the email
#   2. platform_identities.meta_user_id  -> the new id   (claim_execute)
#   3. auth.users.email -> meta_<new>@oculus.device      (GoTrue Admin API)
#
# After that the unmodified login path lands the player in their old account.
# Every step is undone in reverse if a later one fails, so a half-finished
# recovery never leaves someone locked out of both accounts.

PLATFORM_IDENTITIES_TABLE = "platform_identities"
CURRENCY_TABLE = "user_currency"
MIGRATIONS_TABLE = "identity_migrations"

# Meta ids are numeric; anything else is a typo or a paste of the wrong field.
_META_ID_RE = re.compile(r"^[0-9]{1,32}$")


def validate_meta_id(raw: str) -> str:
    """Normalizes a Meta user id typed by staff."""
    cleaned = raw.strip()
    if not _META_ID_RE.match(cleaned):
        raise ValueError(
            f"`{raw.strip()}` doesn't look like a Meta user id. It should be "
            "digits only, e.g. `9328074697317217`."
        )
    return cleaned


def _quoted(value: str) -> str:
    """PostgREST-safe quoted filter value; names contain spaces and commas."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def rpc(session, supabase_url, key, function: str, payload: dict | None = None):
    """POST /rest/v1/rpc/<function>. Same auth and error handling as _rest."""
    return await _rest(
        session, "POST", supabase_url, key, f"rpc/{function}",
        json_body=payload or {},
    )


async def admin_user_id_for_email(session, supabase_url, key, email: str) -> str | None:
    """The auth uuid behind an email, via the same RPC meta-verify uses."""
    result = await rpc(
        session, supabase_url, key, "admin_get_user_id_by_email", {"p_email": email}
    )
    return str(result) if result else None


async def admin_set_email(session, supabase_url, key, user_id: str, email: str) -> None:
    """PUT /auth/v1/admin/users/<id> - GoTrue's admin update.

    Done through GoTrue rather than an UPDATE on auth.users so its own
    bookkeeping (auth.identities) stays consistent.
    """
    endpoint = f"{supabase_url}/auth/v1/admin/users/{user_id}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    async with session.put(
        endpoint, headers=headers, json={"email": email, "email_confirm": True}
    ) as response:
        if response.status not in (200, 201):
            raise SupaError(response.status, await response.text())


async def find_accounts_by_name(
    session, supabase_url, key, display_name: str, limit: int = 8
) -> list[dict]:
    """Old accounts whose in-game name matches, case-insensitively.

    Names are not unique - only 17,119 of 38,633 are - so this returns every
    match and leaves the choosing to staff, who have the ticket in front of them.
    """
    return await _rest(
        session, "GET", supabase_url, key, PROFILES_TABLE,
        params={
            "select": "user_id,display_name,created_at",
            "display_name": f"ilike.{_quoted(display_name.strip())}",
            "limit": str(limit),
        },
    ) or []


async def account_snapshot(session, supabase_url, key, user_id: str) -> dict:
    """Enough about one account for staff to tell two same-named players apart."""
    items = await fetch_inventory(session, supabase_url, key, user_id)

    identity = await _rest(
        session, "GET", supabase_url, key, PLATFORM_IDENTITIES_TABLE,
        params={
            "select": "meta_user_id,verified_at",
            "user_id": f"eq.{user_id}", "platform": "eq.oculus", "limit": "1",
        },
    ) or []

    currency = await _rest(
        session, "GET", supabase_url, key, CURRENCY_TABLE,
        params={"select": "balance", "user_id": f"eq.{user_id}", "limit": "1"},
    ) or []

    migrated = await _rest(
        session, "GET", supabase_url, key, MIGRATIONS_TABLE,
        params={
            "select": "new_meta_user_id,claimed_at,claimed_by",
            "old_user_id": f"eq.{user_id}", "reverted_at": "is.null", "limit": "1",
        },
    ) or []

    return {
        "user_id": user_id,
        "items": items,
        "item_count": len(items),
        "meta_user_id": (identity[0].get("meta_user_id") if identity else None),
        "last_seen": (identity[0].get("verified_at") if identity else None),
        "balance": (currency[0].get("balance") if currency else 0),
        "already_migrated": (migrated[0] if migrated else None),
    }


async def recover_account(
    session, supabase_url, key, *,
    old_user_id: str, new_meta_user_id: str, staff: str,
) -> dict:
    """Move an old account onto a new Meta id. Raises SupaError on refusal.

    Ordering matters and is not arbitrary: auth.users.email is unique, so the
    throwaway account has to let go of the address before the old account can
    take it. Each step records what it needs to undo itself.
    """
    new_email = f"meta_{new_meta_user_id}@oculus.device"

    parked_id = await admin_user_id_for_email(session, supabase_url, key, new_email)
    parked_freed = False

    # 1. Free the email from the throwaway account the new build just made.
    if parked_id and parked_id != old_user_id:
        await admin_set_email(
            session, supabase_url, key, parked_id, f"parked_{parked_id}@apex.local"
        )
        parked_freed = True

    # 2. Database side: repoint the identity and write the audit row.
    try:
        result = await rpc(
            session, supabase_url, key, "claim_execute",
            {
                "p_old_user_id": old_user_id,
                "p_new_meta_user_id": new_meta_user_id,
                "p_via": "discord",
                "p_by": staff,
            },
        )
    except SupaError:
        if parked_freed:
            await admin_set_email(session, supabase_url, key, parked_id, new_email)
        raise

    if not result or not result.get("ok"):
        if parked_freed:
            await admin_set_email(session, supabase_url, key, parked_id, new_email)
        raise SupaError(409, (result or {}).get("error", "claim_execute refused it"))

    # 3. Auth side: the old account takes the new address, which is what makes
    #    the untouched login path land the player in it.
    try:
        await admin_set_email(session, supabase_url, key, old_user_id, new_email)
    except SupaError:
        await rpc(
            session, supabase_url, key, "claim_revert",
            {"p_new_meta_user_id": new_meta_user_id,
             "p_reason": "auth email move failed, rolled back"},
        )
        if parked_freed:
            await admin_set_email(session, supabase_url, key, parked_id, new_email)
        raise

    result["parked_user_id"] = parked_id if parked_freed else None
    return result


async def revert_recovery(
    session, supabase_url, key, *, new_meta_user_id: str, reason: str
) -> dict:
    """Undo one recovery: identity back, email back, audit row closed."""
    result = await rpc(
        session, supabase_url, key, "claim_revert",
        {"p_new_meta_user_id": new_meta_user_id, "p_reason": reason},
    )
    if not result or not result.get("ok"):
        raise SupaError(404, (result or {}).get("error", "no live recovery to revert"))

    restore_email = result.get("restore_email")
    if restore_email and result.get("old_user_id"):
        await admin_set_email(
            session, supabase_url, key, str(result["old_user_id"]), restore_email
        )
    return result


# --- Self-service recovery (Discord-driven, no client changes) ----------------
#
# The old build cannot authenticate any more, so nothing the server writes can
# reach a player's headset - no ban screen, no REDEEM tab, no MOTD. The proof
# has to happen somewhere the player can still reach, which is here.
#
# The player gives their OLD name and their NEW in-game name (read off the
# computer in the new app after logging in once). The old name picks candidate
# accounts, a cosmetics quiz proves which one is theirs, and the new name
# resolves the Meta id to move it onto. No client code, old or new.

# Accounts created within this window are candidates for "the account they just
# made in the new app". Wide enough for someone who installed a few days ago,
# narrow enough that it does not match a five-month-old veteran with the same
# name.
NEW_ACCOUNT_WINDOW_DAYS = 30


async def claim_start(
    session, supabase_url, key, display_name: str, new_meta_user_id: str
) -> dict:
    """Candidate accounts for a remembered name, each with a cosmetics quiz."""
    return await rpc(
        session, supabase_url, key, "claim_start",
        {"p_display_name": display_name, "p_new_meta_user_id": new_meta_user_id},
    ) or {}


async def claim_answer(session, supabase_url, key, attempt_id: str, picked: list[str]) -> dict:
    """Check the three items the player picked. Never reveals the answer."""
    return await rpc(
        session, supabase_url, key, "claim_answer",
        {"p_attempt_id": attempt_id, "p_picked": picked},
    ) or {}


async def find_account_by_exact_name(
    session, supabase_url, key, display_name: str
) -> list[dict]:
    """Recently-created accounts whose name is exactly this.

    Used with a bot-assigned token rather than a name the player chose, so in
    practice this returns one row or none. Still returns a list: two people
    setting the same name is the exact failure this is here to catch, and
    silently taking the first would be how an account gets moved to the wrong
    person.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=NEW_ACCOUNT_WINDOW_DAYS)
    ).isoformat()

    rows = await _rest(
        session, "GET", supabase_url, key, PROFILES_TABLE,
        params={
            "select": "user_id,display_name,created_at",
            "display_name": f"ilike.{_quoted(display_name.strip())}",
            "created_at": f"gte.{cutoff}",
            "limit": "5",
        },
    ) or []

    out = []
    for row in rows:
        uid = str(row.get("user_id"))
        identity = await _rest(
            session, "GET", supabase_url, key, PLATFORM_IDENTITIES_TABLE,
            params={
                "select": "meta_user_id",
                "user_id": f"eq.{uid}", "platform": "eq.oculus", "limit": "1",
            },
        ) or []
        if identity and identity[0].get("meta_user_id"):
            row["meta_user_id"] = identity[0]["meta_user_id"]
            out.append(row)
    return out


async def find_solved_attempt(session, supabase_url, key, rate_key: str) -> dict | None:
    """The most recent quiz this Discord user passed.

    The quiz and the name change happen in two separate commands, possibly
    minutes apart and across a bot restart, so the "they already proved the old
    account" state lives in claim_attempts rather than in bot memory.
    """
    rows = await _rest(
        session, "GET", supabase_url, key, "claim_attempts",
        params={
            "select": "id,candidate_user_id,display_name,created_at",
            "new_meta_user_id": f"eq.{rate_key}",
            "solved": "is.true",
            "order": "created_at.desc",
            "limit": "1",
        },
    ) or []
    return rows[0] if rows else None


async def linked_account_for_discord(
    session, supabase_url, key, discord_id: str
) -> dict | None:
    """The player account already tied to this Discord user, if there is one.

    The link is a by-product of the Discord-cosmetics ticket: the bot minted a
    single-use code against their Discord id, and redeeming it in game recorded
    that account against the code. So

        discord_ticket_claims.code -> code_redemptions.code -> user_id

    is real evidence, not a guess - they held both ends at the time. It is
    strictly stronger than the cosmetics quiz, so anyone who has it can skip
    the quiz entirely.

    Returns None when there is no link, and also when a Discord id somehow
    resolves to more than one account. Every one of the 279 live links is
    currently 1:1, so the plural case means something unexpected - and guessing
    which account to hand over is exactly the mistake worth refusing to make.
    """
    claims = await _rest(
        session, "GET", supabase_url, key, TICKET_CLAIMS_TABLE,
        params={"select": "code", "discord_id": f"eq.{discord_id}"},
    ) or []

    codes = [str(c["code"]) for c in claims if c.get("code")]
    if not codes:
        return None

    redemptions = await _rest(
        session, "GET", supabase_url, key, "code_redemptions",
        params={"select": "user_id,code,redeemed_at", "code": _in_filter(codes)},
    ) or []

    user_ids = {str(r["user_id"]) for r in redemptions if r.get("user_id")}
    if len(user_ids) != 1:
        return None

    user_id = user_ids.pop()

    profile = await fetch_profile(session, supabase_url, key, user_id) or {}
    items = await fetch_inventory(session, supabase_url, key, user_id)

    migrated = await _rest(
        session, "GET", supabase_url, key, MIGRATIONS_TABLE,
        params={
            "select": "new_meta_user_id,claimed_at",
            "old_user_id": f"eq.{user_id}", "reverted_at": "is.null", "limit": "1",
        },
    ) or []

    return {
        "user_id": user_id,
        "display_name": profile.get("display_name"),
        "item_count": len(items),
        "already_migrated": (migrated[0] if migrated else None),
    }
