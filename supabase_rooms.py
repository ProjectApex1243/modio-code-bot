"""Single-responsibility helper: talks to Supabase and nothing else.

Calls the get_active_rooms RPC (see friends_v2_backend_setup.md) which aggregates
the friendpresence table (already kept up to date by update_presence_v2) into
per-room player counts. Requires no game client, PlayFab, or Photon access.
"""

from typing import Any

DEFAULT_ROOM_LIMIT = 10


async def fetch_active_rooms(
    session: "aiohttp.ClientSession",
    supabase_url: str,
    supabase_anon_key: str,
    limit: int = DEFAULT_ROOM_LIMIT,
) -> list[dict[str, Any]]:
    """Fetches the currently active public rooms, sorted by player count descending.

    Each item has: roomId (str), zone (str), region (str), playerCount (int).
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
