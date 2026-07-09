"""Entry point for the bot process. Run with: python bot.py

Handles the /find-active-rooms slash command and a tiny HTTP health endpoint
(required by Render's Web Service health check, since the Discord gateway
connection alone doesn't bind a port). Supabase access lives entirely in
supabase_rooms.py so this file stays focused on Discord + hosting wiring.
"""

import os
import logging

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

from cosmetics import search_cosmetics
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

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
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
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong running that command.")
        else:
            await interaction.response.send_message(
                "Something went wrong running that command.", ephemeral=True
            )


def build_rooms_embed(rooms: list[dict]) -> discord.Embed:
    """Lists every active room (most populated first), split across fields so we
    never blow past Discord's 1024-char-per-field limit."""
    if not rooms:
        return discord.Embed(
            title="No active public rooms right now",
            description="Nobody with public presence enabled is currently in a room.",
            color=EMBED_COLOR,
        )

    total_players = sum(int(room.get("playerCount") or 0) for room in rooms)
    embed = discord.Embed(
        title=f"Active rooms ({len(rooms)})",
        description=f"Total players across all rooms: **{total_players}**",
        color=EMBED_COLOR,
    )

    lines = [
        f"{room['roomId']} — {room['playerCount']} player{'' if room['playerCount'] == 1 else 's'} "
        f"({room.get('region') or 'Unknown'}, {room.get('zone') or 'Unknown'})"
        for room in rooms
    ]

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
            name="Rooms" if index == 0 else f"Rooms (continued, {index + 1})",
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
    for name in ("DISCORD_TOKEN", "SUPABASE_URL", "SUPABASE_ANON_KEY"):
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
