"""Single-responsibility helper: talks to Supabase and nothing else.

Calls the get_active_rooms RPC (see friends_v2_backend_setup.md) which aggregates
the friendpresence table (already kept up to date by update_presence_v2) into
per-room player counts, and reads that same friendpresence table directly to get
the individual player names behind those counts. Requires no game client,
PlayFab, or Photon access.
"""

from datetime import datetime, timezone
from typing import Any

DEFAULT_ROOM_LIMIT = 10

# friendpresence is written by the game client, and the column names have drifted
# between versions, so we accept any of the spellings we've seen rather than
# hard-coding one. Add to these lists if your table uses a different name.
PRESENCE_TABLE = "friendpresence"
PRESENCE_ROOM_KEYS = (
    "roomId",
    "room_id",
    "roomid",
    "room",
    "currentRoom",
    "current_room",
    "roomCode",
    "room_code",
)
PRESENCE_NAME_KEYS = (
    "displayName",
    "display_name",
    "username",
    "user_name",
    "playerName",
    "player_name",
    "nickname",
    "name",
    "playfabId",
    "playfab_id",
    "user_id",
)
PRESENCE_TIMESTAMP_KEYS = (
    "last_seen",
    "lastSeen",
    "last_seen_at",
    "updated_at",
    "updatedAt",
    "heartbeat_at",
    "created_at",
)
# Matches the RPC's own idea of "currently online" — anything older is a player
# who has already left but whose row hasn't been cleaned up yet.
PRESENCE_STALE_SECONDS = 120
PRESENCE_FETCH_LIMIT = 2000


async def fetch_active_rooms(
    session: "aiohttp.ClientSession",
    supabase_url: str,
    supabase_anon_key: str,
    limit: int = DEFAULT_ROOM_LIMIT,
) -> list[dict[str, Any]]:
    """Fetches the currently active public rooms, sorted by player count descending.

    Each item has: roomId (str), zone (str), region (str), playerCount (int), and
    optionally a private join code (privateCode / privCode / roomCode — the RPC has
    used several spellings; main.py accepts any of them).
    """
    endpoint = f"{supabase_url}/rest/v1/rpc/get_active_rooms"
    headers = {
        "Content-Type": "application/json",
        "apikey": supabase_anon_key,
        "Authorization": f"Bearer {supabase_anon_key}",
    }

    async with session.post(endpoint, headers=headers, json={"p_limit": limit}) as response:
        if response.status != 200:
            error_text = await response.text()
            raise RuntimeError(f"get_active_rooms RPC failed ({response.status}): {error_text}")

        rooms = await response.json()
        return rooms if isinstance(rooms, list) else []


def normalize_room_key(room_id: Any) -> str:
    """Room ids come back from the RPC and from friendpresence with inconsistent
    casing/whitespace, so both sides are keyed through this."""
    return str(room_id or "").strip().casefold()


def _first_value(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_stale(row: dict, now: datetime) -> bool:
    """True if the row's heartbeat is older than PRESENCE_STALE_SECONDS.

    Rows with no recognizable timestamp column are kept — better to show a name
    that's a minute out of date than to show an empty room.
    """
    raw = _first_value(row, PRESENCE_TIMESTAMP_KEYS)
    if raw is None:
        return False
    try:
        # Supabase returns ISO 8601; fromisoformat handles the "+00:00" form and
        # (3.11+) the "Z" suffix, but normalize Z anyway for older runtimes.
        seen = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds() > PRESENCE_STALE_SECONDS


async def fetch_room_players(
    session: "aiohttp.ClientSession",
    supabase_url: str,
    api_key: str,
    limit: int = PRESENCE_FETCH_LIMIT,
) -> dict[str, list[str]]:
    """Reads friendpresence and returns {normalized room id: [player names]}.

    This is the per-player detail behind each room's playerCount. Names are
    deduplicated and sorted so the same room renders consistently between runs.
    """
    endpoint = f"{supabase_url}/rest/v1/{PRESENCE_TABLE}"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
    }
    params = {"select": "*", "limit": str(limit)}

    async with session.get(endpoint, headers=headers, params=params) as response:
        if response.status != 200:
            error_text = await response.text()
            raise RuntimeError(
                f"{PRESENCE_TABLE} read failed ({response.status}): {error_text}"
            )
        rows = await response.json()

    if not isinstance(rows, list):
        return {}

    now = datetime.now(timezone.utc)
    players_by_room: dict[str, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or _is_stale(row, now):
            continue
        room_key = normalize_room_key(_first_value(row, PRESENCE_ROOM_KEYS))
        if not room_key:
            continue
        name = _first_value(row, PRESENCE_NAME_KEYS)
        if name is None:
            continue
        players_by_room.setdefault(room_key, set()).add(str(name).strip())

    return {room: sorted(names, key=str.casefold) for room, names in players_by_room.items()}
