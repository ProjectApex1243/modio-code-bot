"""Entry point for the bot process. Run with: python bot.py

Handles the /find-active-rooms slash command and a tiny HTTP health endpoint
(required by Render's Web Service health check, since the Discord gateway
connection alone doesn't bind a port). Supabase access lives entirely in
supabase_rooms.py so this file stays focused on Discord + hosting wiring.
"""

import os
import logging
import re
from datetime import datetime

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

from cosmetics import search_cosmetics
from redeem_codes import (
    create_code,
    generate_code,
    parse_duration,
    resolve_items,
    validate_code,
)
from supabase_rooms import fetch_active_rooms

load_dotenv()

FIND_ACTIVE_ROOMS_COMMAND_NAME = "find-active-rooms"
MAX_LOOKUP_MATCHES = 8
# High cap so we effectively fetch every active room the RPC knows about.
ROOM_FETCH_LIMIT = 1000
# Discord caps a single embed field value at 1024 chars, so we split the
# room list across multiple fields when it gets long.
EMBED_FIELD_CHAR_LIMIT = 1024
EMBED_COLOR = 0x57F287
STAFF_ROLE_NAME = "Staff"
# Redemption-code commands are gated behind this role instead of Staff.
SUPA_MANAGER_ROLE_NAME = "Supa Manager"
RANK_BADGES = ["🥇", "🥈", "🥉"]

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
# Service role key is required for /create-code: redemption_codes has RLS with no
# policies, so only the service role can insert. Keep this key bot-side only.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# Render sets PORT automatically for Web Services; default covers local runs.
HEALTH_CHECK_PORT = int(os.environ.get("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("room-bot")


class RoomBot(discord.Client):
    """Discord client that owns one slash command and one shared HTTP session."""

    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        register_commands(self.tree)

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced commands to guild %s.", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Synced commands globally (may take up to an hour to appear).")

        await start_health_check_server()

    async def close(self) -> None:
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()


def register_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(
        name=FIND_ACTIVE_ROOMS_COMMAND_NAME,
        description="Shows the Most Active Rooms It Can find IN the Game ",
    )
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def find_active_rooms(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        client: RoomBot = interaction.client  # type: ignore[assignment]

        try:
            rooms = await fetch_active_rooms(
                client.http_session, SUPABASE_URL, SUPABASE_ANON_KEY, ROOM_FETCH_LIMIT
            )
            await interaction.followup.send(embed=build_rooms_embed(rooms))
        except Exception as error:
            logger.exception("Error handling /%s", FIND_ACTIVE_ROOMS_COMMAND_NAME)
            # TEMPORARY: surface the real error in Discord for debugging. Revert to the generic
            # message once get_active_rooms is confirmed working end to end.
            await interaction.followup.send(f"Debug — request failed: `{error}`")

    @tree.command(
        name="lookup",
        description="Look up a cosmetic's item ID by name (fuzzy search).",
    )
    @app_commands.describe(cosmetic="Cosmetic name (or part of one), e.g. banana hat")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def lookup(interaction: discord.Interaction, cosmetic: str) -> None:
        matches = search_cosmetics(cosmetic, limit=MAX_LOOKUP_MATCHES)
        if not matches:
            await interaction.response.send_message(
                f"No cosmetic found matching **{cosmetic}**.", ephemeral=True
            )
            return

        top = matches[0]
        embed = discord.Embed(title=top["display_name"], color=EMBED_COLOR)
        # Code block so Discord shows a copy button next to the ID.
        embed.add_field(name="Item ID", value=f"```\n{top['item_id']}\n```", inline=False)
        if top["bundled_items"]:
            embed.add_field(
                name="Bundled items",
                value=" ".join(f"`{item_id}`" for item_id in top["bundled_items"]),
                inline=False,
            )

        similar = matches[1:]
        if similar:
            embed.add_field(
                name="Similar matches",
                value="\n".join(f"{m['display_name']} — `{m['item_id']}`" for m in similar),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @tree.command(
        name="create-code",
        description="Create an in-game redemption code and push it to Supabase.",
    )
    @app_commands.describe(
        items="Cosmetics to grant, comma-separated item IDs or names (use /lookup to find them)",
        duration="How long the code stays live, e.g. 30m, 1h, 2d, 1w (blank = never expires)",
        max_uses="Total number of players that can redeem it (blank = unlimited)",
        code="Custom 8-character code, A-Z/0-9 only (blank = random)",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def create_code_command(
        interaction: discord.Interaction,
        items: str,
        duration: str | None = None,
        max_uses: app_commands.Range[int, 1] | None = None,
        code: str | None = None,
    ) -> None:
        # Ephemeral: anyone who sees a code can redeem it, so only show the creator.
        await interaction.response.defer(ephemeral=True)
        client: RoomBot = interaction.client  # type: ignore[assignment]

        if not SUPABASE_SERVICE_ROLE_KEY:
            await interaction.followup.send(
                "`SUPABASE_SERVICE_ROLE_KEY` is not set on the bot, so it can't "
                "insert codes. Add it to the environment and restart."
            )
            return

        try:
            final_code = validate_code(code) if code else generate_code()
            expires_in = parse_duration(duration) if duration else None
            item_ids, unknown = resolve_items(items)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return

        try:
            row = await create_code(
                client.http_session,
                SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY,
                final_code,
                item_ids,
                max_uses,
                expires_in,
            )
        except RuntimeError as error:
            await interaction.followup.send(str(error))
            return

        embed = discord.Embed(title="✅ Redemption code created", color=EMBED_COLOR)
        # Code block so Discord shows a copy button next to the code.
        embed.add_field(name="Code", value=f"```\n{final_code}\n```", inline=False)
        embed.add_field(
            name="Grants",
            value="\n".join(f"`{item_id}`" for item_id in item_ids),
            inline=False,
        )
        embed.add_field(
            name="Max uses",
            value=str(max_uses) if max_uses else "Unlimited (once per player)",
            inline=True,
        )
        if row.get("expires_at"):
            expiry_unix = int(
                datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).timestamp()
            )
            embed.add_field(
                name="Expires", value=f"<t:{expiry_unix}:f> (<t:{expiry_unix}:R>)", inline=True
            )
        else:
            embed.add_field(name="Expires", value="Never", inline=True)
        if unknown:
            embed.add_field(
                name="⚠️ Not found in cosmetics.json",
                value=(
                    "\n".join(f"`{token}`" for token in unknown)
                    + "\nStored as typed — make sure these match the game's item IDs "
                    "exactly, or they won't grant anything."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @lookup.autocomplete("cosmetic")
    async def lookup_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # Discord allows at most 25 autocomplete choices, 100 chars each.
        return [
            app_commands.Choice(name=m["display_name"][:100], value=m["display_name"][:100])
            for m in search_cosmetics(current, limit=25)
        ]

    @tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingRole):
            await interaction.response.send_message(
                f"You need the **{error.missing_role}** role to use this bot.",
                ephemeral=True,
            )
            return
        logger.exception("Unhandled app command error: %s", error)
        # If the interaction token is gone (restart/expiry), there's nothing we
        # can reply to — just log it instead of cascading into another error.
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong running that command.")
            else:
                await interaction.response.send_message(
                    "Something went wrong running that command.", ephemeral=True
                )
        except discord.NotFound:
            logger.warning("Interaction expired or unknown; could not send error reply.")


def _clean_room_name(name: str) -> str:
    """Strip Unity rich-text markup (e.g. <size=1000%>😎</size> -> 😎) so room
    names render cleanly inside Discord instead of showing raw tags."""
    cleaned = re.sub(r"<[^>]+>", "", name or "").strip()
    return cleaned or (name or "Unknown")


def build_rooms_embed(rooms: list[dict]) -> discord.Embed:
    """Lists every active room (most populated first) with rank badges, split
    across fields so we never blow past Discord's 1024-char-per-field limit."""
    if not rooms:
        return discord.Embed(
            title="💤 No active public rooms right now",
            description="Nobody with public presence enabled is currently in a room.",
            color=EMBED_COLOR,
        )

    total_players = sum(int(room.get("playerCount") or 0) for room in rooms)
    embed = discord.Embed(
        title=f"🎮 Active rooms ({len(rooms)})",
        description=f"👥 **{total_players}** player{'s' if total_players != 1 else ''} online across all rooms",
        color=EMBED_COLOR,
    )

    lines = []
    for rank, room in enumerate(rooms, start=1):
        badge = RANK_BADGES[rank - 1] if rank <= len(RANK_BADGES) else f"`#{rank}`"
        name = _clean_room_name(str(room.get("roomId") or "Unknown"))
        players = int(room.get("playerCount") or 0)
        region = room.get("region") or "Unknown"
        zone = room.get("zone") or "Unknown"
        lines.append(f"{badge} **{name}** — 👥 {players} · 📍 {region} / {zone}")

    # Pack as many room lines as fit into each 1024-char field, then start a new field.
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        # +1 accounts for the "\n" that joins lines together.
        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > EMBED_FIELD_CHAR_LIMIT:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            line_len = len(line)  # no leading newline on the first line of a chunk
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    for index, chunk in enumerate(chunks):
        embed.add_field(
            name="🏠 Rooms" if index == 0 else f"🏠 Rooms (continued, {index + 1})",
            value=chunk,
            inline=False,
        )

    return embed


async def start_health_check_server() -> None:
    """Binds an HTTP port so Render's Web Service health check passes."""
    app = web.Application()
    app.router.add_get("/", lambda _request: web.Response(text="ok"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=HEALTH_CHECK_PORT)
    await site.start()
    logger.info("Health check server listening on port %s.", HEALTH_CHECK_PORT)


def main() -> None:
    # TEMPORARY: reports which required env vars the process can actually see, without leaking
    # their values, so a Render dashboard/env-var mismatch shows up immediately in the logs.
    for name in ("DISCORD_TOKEN", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        logger.info("%s is %s", name, "set" if os.environ.get(name) else "MISSING")

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not set.")
    if not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_ANON_KEY is not set.")

    bot = RoomBot()
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
