"""Entry point for the bot process. Run with: python bot.py

Handles the /find-active-rooms slash command and a tiny HTTP health endpoint
(required by Render's Web Service health check, since the Discord gateway
connection alone doesn't bind a port). Supabase access lives entirely in
supabase_rooms.py so this file stays focused on Discord + hosting wiring.
"""

import asyncio
import os
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

import supa_admin
import tickets
from cosmetics import COSMETICS, display_name_for, image_path, search_cosmetics
from redeem_codes import (
    create_code,
    generate_code,
    parse_duration,
    resolve_items,
    validate_code,
)
from supabase_rooms import fetch_active_rooms, fetch_room_players, normalize_room_key

load_dotenv()

FIND_ACTIVE_ROOMS_COMMAND_NAME = "find-active-rooms"
MAX_LOOKUP_MATCHES = 8
# High cap so we effectively fetch every active room the RPC knows about.
ROOM_FETCH_LIMIT = 1000
# Discord caps a single embed field value at 1024 chars, so we split the
# room list across multiple fields when it gets long.
EMBED_FIELD_CHAR_LIMIT = 1024
# Names listed under each room before we collapse the rest into "+N more".
MAX_PLAYER_NAMES_PER_ROOM = 12
# Discord rejects an embed whose total content passes 6000 chars, so with 1024-char
# fields we can afford five of them plus the title and description.
MAX_EMBED_FIELDS = 5
EMBED_COLOR = 0x57F287
# Read from the environment so main.py and tickets.py can't drift apart if the
# role is ever renamed.
STAFF_ROLE_NAME = os.environ.get("STAFF_ROLE_NAME", "Staff")
# Redemption-code commands are gated behind this role instead of Staff.
SUPA_MANAGER_ROLE_NAME = "Supa Manager"
# Role that /prune-unverified targets. Owner-only, so it isn't gated by a role check.
UNVERIFIED_ROLE_NAME = "Unverified"
# Seconds between kicks. Discord's per-guild kick bucket is tight; a small gap
# keeps a large prune from stalling on 429s.
PRUNE_KICK_DELAY = 1.0
# How long confirmation buttons stay clickable.
CONFIRM_TIMEOUT = 120
# /leaderboard default and cap (the manual SQL used LIMIT 50).
LEADERBOARD_DEFAULT = 10
LEADERBOARD_MAX = 50
EMBED_RED = 0xED4245
# Sea green of the in-game cosmetic card header, so /lookup matches the store UI.
EMBED_COSMETIC = 0x4E8877
CURRENCY_NAME = "Shiny Rocks"
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
        # members intent is privileged: /prune-unverified can't list who holds a
        # role without it. Must also be toggled on in the Discord developer portal
        # (Bot -> Privileged Gateway Intents -> Server Members Intent).
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        register_commands(self.tree)
        # Must happen before the gateway connects, or ticket buttons posted
        # before the last restart come back dead.
        tickets.register_persistent_views(self)
        logger.info(
            "Ticket buttons registered (%s). Reward items: %s",
            ", ".join(tickets.registered_custom_ids()),
            ", ".join(tickets.cosmetic_items()),
        )
        await tickets.self_test(self.http_session)

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


class ConfirmView(discord.ui.View):
    """Yes/no buttons shown before a destructive action runs. Only the member
    who ran the command can press them."""

    def __init__(self, invoker_id: int, confirm_label: str, working_text: str) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.invoker_id = invoker_id
        self.confirmed = False
        self.working_text = working_text
        # The decorator below needs a literal label, so the caller's wording is
        # applied to the built button here.
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.style == discord.ButtonStyle.danger:
                child.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This confirmation isn't yours.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = True
        await interaction.response.edit_message(
            content=self.working_text, embed=None, view=None
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class RecoverQuizView(discord.ui.View):
    """The "pick the 3 cosmetics you owned" gate for /recover.

    Short-lived and ephemeral on purpose. It must never be registered as a
    persistent view — see tickets.help_menu_embed for what that does to the
    dispatch table — and the attempt behind it expires server-side after 30
    minutes regardless.
    """

    def __init__(
        self, *, attempt_id: str, options: list[str], new_meta_id: str, invoker_id: int
    ) -> None:
        super().__init__(timeout=600)
        self.attempt_id = attempt_id
        self.new_meta_id = new_meta_id
        self.invoker_id = invoker_id
        self.select = discord.ui.Select(
            placeholder="Pick the 3 cosmetics you owned",
            min_values=3,
            max_values=3,
            options=[
                discord.SelectOption(label=_item_label(o)[:100], value=o[:100])
                for o in options
            ],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "This isn't your recovery.", ephemeral=True
            )
            return False
        return True

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        client: RoomBot = interaction.client  # type: ignore[assignment]
        picked = list(self.select.values)

        try:
            verdict = await supa_admin.claim_answer(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                self.attempt_id, picked,
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(f"Something broke: `{error}`", ephemeral=True)
            return

        if not verdict.get("ok"):
            code = verdict.get("error")
            if code == "WRONG":
                left = int(verdict.get("tries_left") or 0)
                text = (
                    f"Not right — **{left}** {'try' if left == 1 else 'tries'} left."
                    if left
                    else "That was the last try."
                )
            elif code == "TOO_MANY_TRIES":
                text = "Too many wrong guesses. Open a ticket and staff will help."
            elif code == "EXPIRED":
                text = "This took too long. Run `/recover` again."
            else:
                text = "That didn't work. Open a ticket and staff will help."
            await interaction.followup.send(text, ephemeral=True)
            return

        # Passed. Resolve the account behind the attempt and do the swap.
        try:
            old_user_id = await supa_admin.claim_attempt_account(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                self.attempt_id,
            )
            if not old_user_id:
                raise supa_admin.SupaError(500, "attempt vanished")
            result = await supa_admin.recover_account(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                old_user_id=old_user_id, new_meta_user_id=self.new_meta_id,
                staff=f"self-service quiz ({interaction.user})",
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(
                "You got the quiz right, but the move itself failed — "
                f"**nothing was changed**. Open a ticket and quote this:\n`{error}`",
                ephemeral=True,
            )
            return

        self.select.disabled = True
        self.stop()
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Got it back",
                description=(
                    "Your old account is now yours on the new app.\n\n"
                    "**Fully close the game and open it again** — your cosmetics "
                    "will be there when it loads."
                ),
                color=EMBED_COLOR,
            ),
            ephemeral=True,
        )
        logger.info(
            "Self-service recovery: %s -> Meta id %s (by %s)",
            old_user_id, self.new_meta_id, interaction.user,
        )


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
            # Who's in each room is a nice-to-have on top of the counts, and
            # friendpresence may be locked down by RLS, so a failure here only
            # costs us the name lists — the room list still goes out.
            players_by_room: dict[str, list[str]] = {}
            try:
                players_by_room = await fetch_room_players(
                    client.http_session,
                    SUPABASE_URL,
                    SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY,
                )
            except Exception:
                logger.exception("Could not read player names from friendpresence")
            await interaction.followup.send(embed=build_rooms_embed(rooms, players_by_room))
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

        embed, files = build_cosmetic_embed(matches[0])

        similar = matches[1:]
        if similar:
            embed.add_field(
                name="Similar matches",
                value="\n".join(f"{m['display_name']} — `{m['item_id']}`" for m in similar),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, files=files)

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

    @tree.command(
        name="disable-code",
        description="Kill a redemption code instantly (sets enabled = false).",
    )
    @app_commands.describe(code="The code to disable, e.g. APEX2026")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def disable_code_command(interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        cleaned = code.strip().upper()
        if not cleaned:
            await interaction.followup.send("Give me a code to disable.")
            return
        try:
            found = await supa_admin.disable_code(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, cleaned
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        if found:
            await interaction.followup.send(f"🚫 Code `{cleaned}` is now disabled.")
        else:
            await interaction.followup.send(f"No code named `{cleaned}` exists.")

    @tree.command(
        name="ban",
        description="Ban a player by UUID — permanent, or timed if you give a duration.",
    )
    @app_commands.describe(
        user_id="Player's user UUID",
        reason="Why they're banned",
        duration="Ban length, e.g. 24h, 7d, 1w (blank = permanent)",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def ban_command(
        interaction: discord.Interaction,
        user_id: str,
        reason: str,
        duration: str | None = None,
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            ban_length = parse_duration(duration) if duration else None
        except ValueError as error:
            await interaction.followup.send(str(error))
            return
        # The RPC takes whole hours, so anything under an hour becomes one hour —
        # say so rather than quietly serving a longer ban than was asked for.
        rounded_note = None
        if ban_length is not None:
            hours = supa_admin.duration_to_hours(ban_length)
            if abs(ban_length.total_seconds() - hours * 3600) > 1:
                rounded_note = (
                    f"Bans are stored in whole hours, so `{duration}` was rounded "
                    f"to **{hours}h**."
                )

        already_banned = None
        try:
            already_banned = await supa_admin.fetch_ban(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
            row = await supa_admin.ban_player(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                uid, reason, ban_length,
            )
        except supa_admin.SupaError as error:
            # banned_players.user_id is a foreign key into auth.users, so a UUID
            # that isn't a real account fails here rather than on validation.
            if "foreign key" in error.detail.lower() or "23503" in error.detail:
                await interaction.followup.send(
                    f"No player account exists with the ID `{uid}` — double-check the UUID."
                )
            else:
                await interaction.followup.send(str(error))
            return

        row = row or {}
        embed = discord.Embed(
            title="🔨 Ban updated" if already_banned else "🔨 Player banned",
            color=EMBED_RED,
        )
        embed.add_field(name="Player", value=f"`{uid}`", inline=False)
        embed.add_field(name="Reason", value=row.get("reason") or reason, inline=False)
        if row.get("banned_until") and not row.get("is_permanent"):
            unix = int(
                datetime.fromisoformat(
                    str(row["banned_until"]).replace("Z", "+00:00")
                ).timestamp()
            )
            embed.add_field(name="Until", value=f"<t:{unix}:f> (<t:{unix}:R>)", inline=False)
        else:
            embed.add_field(name="Until", value="Permanent", inline=False)
        if already_banned:
            embed.set_footer(text="They were already banned — this replaced the old ban.")
        await interaction.followup.send(embed=embed, content=rounded_note)

    @tree.command(name="unban", description="Unban a player by their user UUID.")
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def unban_command(interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            was_banned = await supa_admin.unban_player(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if was_banned:
            await interaction.followup.send(f"✅ Unbanned `{uid}`.")
        else:
            await interaction.followup.send(f"`{uid}` wasn't banned.")

    @tree.command(name="banned-list", description="Show every banned player.")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def banned_list_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            rows = await supa_admin.list_banned(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        if not rows:
            await interaction.followup.send("Nobody is banned right now. 🎉")
            return
        embed = discord.Embed(
            title=f"🔨 Banned players ({len(rows)})",
            description=_joined_lines([_format_ban(row) for row in rows]),
            color=EMBED_RED,
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="ban-leaderboard",
        description="Show who has issued the most bans.",
    )
    @app_commands.describe(top="How many to show (default 10, max 50)")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def ban_leaderboard_command(
        interaction: discord.Interaction,
        top: app_commands.Range[int, 1, LEADERBOARD_MAX] = LEADERBOARD_DEFAULT,
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            counts, unattributed = await supa_admin.fetch_ban_counts_by_issuer(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
            )
            # Ties break on the id so the same query twice gives the same order.
            ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:top]
            # Only the names actually being rendered are looked up, so this stays
            # one small request however many staff have ever issued a ban.
            names = await supa_admin.fetch_profile_names(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                [issuer for issuer, _ in ranked],
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        if not ranked:
            await interaction.followup.send("No ban has an issuer recorded yet.")
            return

        lines = []
        for rank, (issuer, count) in enumerate(ranked, start=1):
            badge = RANK_BADGES[rank - 1] if rank <= len(RANK_BADGES) else f"`#{rank}`"
            profile_name = names.get(issuer, "").strip()
            who = (
                discord.utils.escape_markdown(_clean_room_name(profile_name))
                if profile_name
                else "Unknown"
            )
            plural = "ban" if count == 1 else "bans"
            # Id on its own line under the name: it's what staff copy into the
            # other ban commands, and inline it would crowd out the count.
            lines.append(f"{badge} **{who}** — {count:,} {plural}\n　└ `{issuer}`")

        embed = discord.Embed(
            title=f"🔨 Ban leaderboard — top {len(ranked)}",
            description=_joined_lines(lines),
            color=EMBED_RED,
        )
        # The unattributed count is worth showing: those bans are real, they just
        # predate banned_by or were issued by the game rather than by a person.
        footer = f"{sum(counts.values()):,} bans across {len(counts)} staff"
        if unattributed:
            footer += f" · {unattributed:,} more with no issuer recorded"
        embed.set_footer(text=footer)
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @tree.command(
        name="check-ban-length",
        description="Check how long is left on a player's ban.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def check_ban_length_command(
        interaction: discord.Interaction, user_id: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            row = await supa_admin.fetch_ban(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if row is None:
            await interaction.followup.send(f"`{uid}` isn't banned. 🎉")
            return

        until = _ban_expiry(row)
        if until is None:
            if row.get("is_permanent") or not row.get("banned_until"):
                headline = "**Permanent** — there's no clock on this one."
            else:
                # Only reachable if banned_until holds something that isn't a
                # timestamp; say so instead of calling it permanent.
                headline = (
                    f"Ends `{row['banned_until']}`, which I couldn't read as a "
                    "date — check the row in Supabase."
                )
            color = EMBED_RED
        else:
            unix = int(until.timestamp())
            remaining = until - datetime.now(timezone.utc)
            if remaining.total_seconds() <= 0:
                # The row stays behind once the clock runs out, so an expired ban
                # still shows up here until someone clears it.
                headline = (
                    f"**Expired** <t:{unix}:R> — the ban is over, but the row is "
                    "still there. Use `/unban` to clear it."
                )
                color = EMBED_COLOR
            else:
                headline = (
                    f"**{_format_remaining(remaining)}** left "
                    f"(ends <t:{unix}:f>, <t:{unix}:R>)."
                )
                color = EMBED_RED

        embed = discord.Embed(title="⏳ Ban length", description=headline, color=color)
        embed.add_field(name="Player", value=f"`{uid}`", inline=False)
        embed.add_field(
            name="Reason",
            value=str(row.get("reason") or "no reason recorded").strip(),
            inline=False,
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="username",
        description="Look up a player's in-game name from their user UUID.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def username_command(interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            profile = await supa_admin.fetch_profile(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return

        # friendpresence is written by the game client rather than the account
        # system, so it can still know a name for a UUID with no profile row — and
        # it's the only table that knows when they were last online. Losing it
        # shouldn't sink a lookup that already has the profile name.
        presence: dict | None = None
        try:
            presence = await supa_admin.fetch_presence_row(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except supa_admin.SupaError:
            logger.exception("Could not read presence for %s", uid)

        name = str((profile or {}).get("display_name") or "").strip()
        source = "current profile name"
        if not name and presence:
            name = str(_first_row_value(presence, _PRESENCE_NAME_KEYS) or "").strip()
            source = "last name the game reported"
        if not name:
            await interaction.followup.send(
                f"No player found with the ID `{uid}` — double-check the UUID."
            )
            return

        embed = discord.Embed(
            title="🔎 Player lookup",
            description=f"## {discord.utils.escape_markdown(_clean_room_name(name))}",
            color=EMBED_COLOR,
        )
        embed.add_field(name="User ID", value=f"`{uid}`", inline=False)
        embed.add_field(name="Name source", value=source, inline=False)
        created = _parse_timestamp((profile or {}).get("created_at"))
        if created is not None:
            embed.add_field(
                name="Profile created",
                value=f"<t:{int(created.timestamp())}:D> (<t:{int(created.timestamp())}:R>)",
                inline=False,
            )
        if presence:
            seen = _parse_timestamp(_first_row_value(presence, _PRESENCE_SEEN_KEYS))
            where = _first_row_value(presence, _PRESENCE_ROOM_KEYS)
            last_seen = f"<t:{int(seen.timestamp())}:R>" if seen is not None else "unknown"
            if where:
                last_seen += f" in room `{where}`"
            embed.add_field(name="Last seen", value=last_seen, inline=False)
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @tree.command(
        name="give-ban-perms",
        description="Give a player in-game ban permissions.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def give_ban_perms_command(interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            added = await supa_admin.give_ban_perms(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if added:
            await interaction.followup.send(f"✅ `{uid}` now has ban permissions.")
        else:
            await interaction.followup.send(f"`{uid}` already has ban permissions.")

    @tree.command(
        name="remove-ban-perms",
        description="Take a player's in-game ban permissions away.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def remove_ban_perms_command(
        interaction: discord.Interaction, user_id: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            removed = await supa_admin.remove_ban_perms(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if removed:
            await interaction.followup.send(f"✅ Removed ban permissions from `{uid}`.")
        else:
            await interaction.followup.send(f"`{uid}` didn't have ban permissions.")

    @tree.command(
        name="ban-perms-list",
        description="Show every player with in-game ban permissions.",
    )
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def ban_perms_list_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            rows = await supa_admin.list_ban_perms(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        if not rows:
            await interaction.followup.send("No players have ban permissions.")
            return
        embed = discord.Embed(
            title=f"🛡️ Players with ban perms ({len(rows)})",
            description=_joined_lines(
                [f"`{row.get('user_id', '?')}`" for row in rows]
            ),
            color=EMBED_COLOR,
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="give-cosmetic",
        description="Give a player one or more cosmetics.",
    )
    @app_commands.describe(
        user_id="Player's user UUID",
        items="Cosmetics to grant, comma-separated item IDs or names (use /lookup to find them)",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def give_cosmetic_command(
        interaction: discord.Interaction, user_id: str, items: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            item_ids, unknown = resolve_items(items)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return
        try:
            granted, already = await supa_admin.grant_items(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid, item_ids
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        parts = []
        if granted:
            parts.append(
                f"✅ Gave `{uid}`: " + ", ".join(_item_label(i) for i in granted)
            )
        if already:
            parts.append(
                "Already owned (skipped): " + ", ".join(_item_label(i) for i in already)
            )
        if unknown:
            parts.append(
                "⚠️ Not in cosmetics.json, stored as typed: "
                + ", ".join(f"`{token}`" for token in unknown)
            )
        await interaction.followup.send("\n".join(parts))

    @tree.command(
        name="remove-cosmetic",
        description="Take one or more cosmetics away from a player.",
    )
    @app_commands.describe(
        user_id="Player's user UUID",
        items="Cosmetics to remove, comma-separated item IDs or names",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def remove_cosmetic_command(
        interaction: discord.Interaction, user_id: str, items: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            item_ids, _unknown = resolve_items(items)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return
        try:
            removed, not_owned = await supa_admin.remove_items(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid, item_ids
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        parts = []
        if removed:
            parts.append(
                f"🗑️ Removed from `{uid}`: " + ", ".join(_item_label(i) for i in removed)
            )
        if not_owned:
            parts.append(
                "They didn't own: " + ", ".join(_item_label(i) for i in not_owned)
            )
        await interaction.followup.send("\n".join(parts))

    @tree.command(
        name="give-all-cosmetics",
        description="Give a player every cosmetic in the game catalog.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def give_all_cosmetics_command(
        interaction: discord.Interaction, user_id: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return
        try:
            catalog = await supa_admin.fetch_catalog_item_ids(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
            )
            if not catalog:
                await interaction.followup.send(
                    "Couldn't read the game catalog from `title_data` — no items granted."
                )
                return
            granted, already = await supa_admin.grant_items(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid, catalog
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        await interaction.followup.send(
            f"🎁 Gave `{uid}` **{len(granted)}** new cosmetic(s) "
            f"({len(already)} already owned) out of {len(catalog)} in the catalog."
        )

    @tree.command(
        name="clear-inventory",
        description="Delete a player's ENTIRE cosmetic inventory.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def clear_inventory_command(
        interaction: discord.Interaction, user_id: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            owned = await supa_admin.fetch_inventory(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if not owned:
            await interaction.followup.send(f"`{uid}`'s inventory is already empty.")
            return

        confirm_embed = discord.Embed(
            title="⚠️ Confirm inventory wipe",
            description=(
                f"This will delete **all {len(owned)}** cosmetic(s) that `{uid}` owns. "
                "This cannot be undone."
            ),
            color=EMBED_RED,
        )
        view = ConfirmView(interaction.user.id, "Wipe inventory", "Wiping…")
        await interaction.followup.send(embed=confirm_embed, view=view)
        if await view.wait():
            await interaction.followup.send("Timed out — nothing was deleted.")
            return
        if not view.confirmed:
            # ConfirmView already replaced the message with "Cancelled."
            return
        try:
            deleted = await supa_admin.clear_inventory(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        await interaction.followup.send(
            f"🗑️ Deleted `{uid}`'s inventory — {deleted} row(s) removed."
        )

    @tree.command(
        name="inventory",
        description="Show every cosmetic a player owns.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def inventory_command(interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            owned = await supa_admin.fetch_inventory(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if not owned:
            await interaction.followup.send(f"`{uid}` owns no cosmetics.")
            return
        embed = discord.Embed(
            title=f"🎒 Inventory ({len(owned)} items)",
            description=_joined_lines([_item_label(item_id) for item_id in owned]),
            color=EMBED_COSMETIC,
        )
        embed.set_footer(text=uid)
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="reset-score",
        description="Reset a player's leaderboard tag count to 0.",
    )
    @app_commands.describe(user_id="Player's user UUID")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def reset_score_command(interaction: discord.Interaction, user_id: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            uid = supa_admin.validate_user_id(user_id)
            found = await supa_admin.reset_score(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
        except (ValueError, supa_admin.SupaError) as error:
            await interaction.followup.send(str(error))
            return
        if found:
            await interaction.followup.send(f"🏷️ Reset `{uid}`'s tag count to 0.")
        else:
            await interaction.followup.send(f"`{uid}` has no leaderboard row.")

    @tree.command(
        name="leaderboard",
        description="Show the top players by total tags.",
    )
    @app_commands.describe(top="How many players to show (default 10, max 50)")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def leaderboard_command(
        interaction: discord.Interaction,
        top: app_commands.Range[int, 1, LEADERBOARD_MAX] = LEADERBOARD_DEFAULT,
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        try:
            rows = await supa_admin.fetch_leaderboard(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, top
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return
        if not rows:
            await interaction.followup.send("The leaderboard is empty.")
            return
        embed = discord.Embed(
            title=f"🏆 Tag leaderboard — top {len(rows)}",
            description=_joined_lines(
                [_leaderboard_line(rank, row) for rank, row in enumerate(rows, start=1)]
            ),
            color=EMBED_COLOR,
        )
        await interaction.followup.send(embed=embed)

    @tree.command(
        name="ticket-panel",
        description="Post the support panel players use to open tickets.",
    )
    @app_commands.describe(
        channel="Where to post it (blank = right here)",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def ticket_panel_command(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send("Pick a normal text channel for the panel.")
            return

        permissions = target.permissions_for(interaction.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.followup.send(
                f"I can't post in {target.mention} — I need **Send Messages** and "
                "**Embed Links** there."
            )
            return

        await target.send(embed=tickets.panel_embed(), view=tickets.TicketPanelView())

        notes = []
        if not interaction.guild.me.guild_permissions.manage_channels:
            notes.append(
                "⚠️ I don't have **Manage Channels**, so ban-appeal and other "
                "tickets can't open a channel until that's granted."
            )
        if discord.utils.get(interaction.guild.roles, name=STAFF_ROLE_NAME) is None:
            notes.append(
                f"⚠️ No **{STAFF_ROLE_NAME}** role exists, so staff won't be "
                "added to new tickets — only the person who opened one could see it."
            )
        if not SUPABASE_SERVICE_ROLE_KEY:
            notes.append(
                "⚠️ `SUPABASE_SERVICE_ROLE_KEY` isn't set, so the cosmetics claim "
                "will refuse to hand out codes."
            )
        await interaction.followup.send(
            f"Panel posted in {target.mention}."
            + ("\n\n" + "\n".join(notes) if notes else "")
        )

    @tree.command(
        name="reset-cosmetic-claim",
        description="Let someone claim the Discord cosmetics again.",
    )
    @app_commands.describe(user="The member whose claim should be cleared")
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def reset_cosmetic_claim_command(
        interaction: discord.Interaction, user: discord.User
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]
        discord_id = str(user.id)
        try:
            claim = await supa_admin.fetch_ticket_claim(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, discord_id
            )
            if claim is None:
                await interaction.followup.send(
                    f"{user.mention} hasn't claimed the Discord cosmetics yet.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await supa_admin.release_ticket_claim(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, discord_id
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return

        old_code = claim.get("code")
        await interaction.followup.send(
            f"✅ Cleared {user.mention}'s cosmetics claim — they can claim again."
            + (
                f"\nTheir old code was `{old_code}`; it still works unless you "
                f"run `/disable-code {old_code}`."
                if old_code
                else ""
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- Account recovery (App Lab move) -------------------------------------
    # Meta gives every app a different id for the same person, so a player on
    # the new build looks like a stranger. Nothing was lost - these commands
    # repoint the new Meta id at the account that already exists.

    @tree.command(
        name="recover-lookup",
        description="Find a player's old account by their in-game name.",
    )
    @app_commands.describe(name="The player's old in-game name, spelled as it was")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def recover_lookup(interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]

        try:
            matches = await supa_admin.find_accounts_by_name(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, name
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error))
            return

        if not matches:
            await interaction.followup.send(
                f"No account found with the name **{discord.utils.escape_markdown(name)}**. "
                "Names are matched exactly (case doesn't matter) — ask them to "
                "check the spelling."
            )
            return

        embed = discord.Embed(
            title="🎒 Account recovery — candidates",
            description=(
                f"**{len(matches)}** account(s) named "
                f"**{discord.utils.escape_markdown(name)}**.\n"
                "Names are not unique, so confirm with the cosmetics and rock "
                "count before running `/recover-account`."
            ),
            color=EMBED_COLOR,
        )

        # Snapshots are four REST calls each, so only the first few are detailed.
        for row in matches[:5]:
            uid = str(row.get("user_id"))
            try:
                snap = await supa_admin.account_snapshot(
                    client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
                )
            except supa_admin.SupaError:
                logger.exception("Snapshot failed for %s", uid)
                embed.add_field(name=f"`{uid}`", value="(couldn't read details)", inline=False)
                continue

            sample = ", ".join(_item_label(i) for i in snap["items"][:6]) or "nothing"
            lines = [
                f"**{snap['item_count']}** cosmetics · **{snap['balance']}** {CURRENCY_NAME}",
                f"Owns: {sample}",
                f"Meta id: `{snap['meta_user_id'] or 'unknown'}`",
            ]
            seen = _parse_timestamp(snap.get("last_seen"))
            if seen is not None:
                lines.append(f"Last login: <t:{int(seen.timestamp())}:R>")
            if snap["already_migrated"]:
                lines.append(
                    "⚠️ **Already recovered** onto Meta id "
                    f"`{snap['already_migrated'].get('new_meta_user_id')}`"
                )
            embed.add_field(name=f"`{uid}`", value="\n".join(lines), inline=False)

        if len(matches) > 5:
            embed.set_footer(text=f"+{len(matches) - 5} more not shown")

        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @tree.command(
        name="recover-account",
        description="Put a player's old account back on their new Meta id.",
    )
    @app_commands.describe(
        old_user_id="The old account's player UUID (from /recover-lookup)",
        new_meta_id="Their Meta user id on the new app (digits only)",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def recover_account_command(
        interaction: discord.Interaction, old_user_id: str, new_meta_id: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]

        try:
            uid = supa_admin.validate_user_id(old_user_id)
            meta_id = supa_admin.validate_meta_id(new_meta_id)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return

        try:
            snap = await supa_admin.account_snapshot(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, uid
            )
            result = await supa_admin.recover_account(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                old_user_id=uid, new_meta_user_id=meta_id,
                staff=str(interaction.user),
            )
        except supa_admin.SupaError as error:
            # claim_execute refuses rather than half-doing it, and every step
            # rolls itself back, so nobody is stranded when this happens.
            await interaction.followup.send(
                f"❌ Recovery didn't go through — **nothing was changed**.\n`{error}`"
            )
            return

        embed = discord.Embed(
            title="🎒 Account recovered",
            description=(
                f"**{snap['item_count']}** cosmetics and **{snap['balance']}** "
                f"{CURRENCY_NAME} are now on Meta id `{meta_id}`."
            ),
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Old account", value=f"`{uid}`", inline=False)
        embed.add_field(
            name="Previous Meta id",
            value=f"`{result.get('old_meta_user_id') or 'unknown'}`",
            inline=False,
        )
        embed.add_field(
            name="Tell the player",
            value=(
                "**Fully close the game and open it again.** Their cosmetics "
                "will be there when it loads."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Recovered by {interaction.user}")
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )
        logger.info("Recovered %s onto Meta id %s (by %s)", uid, meta_id, interaction.user)

    @tree.command(
        name="recover-undo",
        description="Undo a recovery that went to the wrong person.",
    )
    @app_commands.describe(
        new_meta_id="The Meta id the account was moved onto",
        reason="Why it's being undone",
    )
    @app_commands.checks.has_role(SUPA_MANAGER_ROLE_NAME)
    async def recover_undo(
        interaction: discord.Interaction, new_meta_id: str, reason: str
    ) -> None:
        await interaction.response.defer()
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]

        try:
            meta_id = supa_admin.validate_meta_id(new_meta_id)
        except ValueError as error:
            await interaction.followup.send(str(error))
            return

        try:
            result = await supa_admin.revert_recovery(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                new_meta_user_id=meta_id, reason=f"{reason} (by {interaction.user})",
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(f"❌ Couldn't undo it.\n`{error}`")
            return

        await interaction.followup.send(
            f"✅ Undone. Account `{result.get('old_user_id')}` is back on its "
            "original Meta id and can be recovered again.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        logger.info("Reverted recovery for Meta id %s (by %s)", meta_id, interaction.user)

    @tree.command(
        name="recover",
        description="Get your old cosmetics onto your new account. No staff needed.",
    )
    @app_commands.describe(
        old_name="Your OLD in-game name, spelled exactly as it was",
        new_name="Your NEW in-game name, from the computer in the new app",
    )
    async def recover_self_serve(
        interaction: discord.Interaction, old_name: str, new_name: str
    ) -> None:
        # Deliberately not role-gated: this is the only route players have.
        # The quiz is the gate, and claim_start rate-limits by target account.
        await interaction.response.defer(ephemeral=True)
        if not await _ensure_service_key(interaction):
            return
        client: RoomBot = interaction.client  # type: ignore[assignment]

        try:
            targets = await supa_admin.find_new_account(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, new_name
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return

        if not targets:
            await interaction.followup.send(
                "I can't find a **new** account called "
                f"**{discord.utils.escape_markdown(new_name)}**.\n\n"
                "Install the new app and log in once first — that's what creates "
                "the account I move your cosmetics onto. Then check the name on "
                "the computer and run this again.",
                ephemeral=True,
            )
            return
        if len(targets) > 1:
            await interaction.followup.send(
                f"There's more than one new account called "
                f"**{discord.utils.escape_markdown(new_name)}**, so I can't tell "
                "which is yours. Change your name in the new app to something "
                "unusual, then run this again — or open a ticket.",
                ephemeral=True,
            )
            return

        target = targets[0]
        try:
            started = await supa_admin.claim_start(
                client.http_session, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                old_name, str(target["meta_user_id"]),
            )
        except supa_admin.SupaError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return

        if started.get("error") == "RATE_LIMITED":
            await interaction.followup.send(
                "You've tried a lot of names in a short time. Wait an hour, or "
                "open a ticket and staff will sort it out.", ephemeral=True,
            )
            return

        candidates = started.get("candidates") or []
        if not candidates:
            await interaction.followup.send(
                "No account with cosmetics is named "
                f"**{discord.utils.escape_markdown(old_name)}**.\n\n"
                "It has to be spelled the way it was in game. If it's right and "
                "this still fails, the account may already have been recovered — "
                "open a ticket.",
                ephemeral=True,
            )
            return

        # One quiz at a time. Same-named accounts are walked in order, which is
        # rare enough not to be worth a picker.
        first = candidates[0]
        await interaction.followup.send(
            embed=discord.Embed(
                title="🎒 Prove it's your account",
                description=(
                    f"Found an account named **{discord.utils.escape_markdown(str(first['name']))}** "
                    f"with **{first['item_count']}** cosmetics and "
                    f"**{first['balance']}** {CURRENCY_NAME}.\n\n"
                    "**Pick the 3 cosmetics you owned.** You get 3 tries."
                ),
                color=EMBED_COLOR,
            ),
            view=RecoverQuizView(
                attempt_id=str(first["attempt_id"]),
                options=[str(o) for o in first["options"]],
                new_meta_id=str(target["meta_user_id"]),
                invoker_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    @tree.command(
        name="prune-unverified",
        description=f"Kick every member with the {UNVERIFIED_ROLE_NAME} role. Server owner only.",
    )
    @app_commands.describe(
        joined_days_ago="Only kick members who joined at least this many days ago (blank = all)",
        preview="Just show who would be kicked, without kicking anyone",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def prune_unverified(
        interaction: discord.Interaction,
        joined_days_ago: app_commands.Range[int, 0] | None = None,
        preview: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works inside a server.", ephemeral=True
            )
            return
        # Owner-only by design: this can empty a large chunk of the member list.
        if interaction.user.id != guild.owner_id:
            await interaction.response.send_message(
                "Only the **server owner** can use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        role = discord.utils.get(guild.roles, name=UNVERIFIED_ROLE_NAME)
        if role is None:
            await interaction.followup.send(
                f"No role named **{UNVERIFIED_ROLE_NAME}** exists in this server."
            )
            return
        if not guild.me.guild_permissions.kick_members:
            await interaction.followup.send(
                "I don't have the **Kick Members** permission in this server."
            )
            return

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=joined_days_ago)
            if joined_days_ago
            else None
        )
        # fetch_members hits the gateway directly, so this works even if the member
        # cache hasn't finished chunking a big guild yet.
        kickable: list[discord.Member] = []
        skipped_hierarchy = 0
        skipped_recent = 0
        skipped_bots = 0
        async for member in guild.fetch_members(limit=None):
            if role not in member.roles:
                continue
            if member.bot:
                skipped_bots += 1
                continue
            if cutoff and (member.joined_at is None or member.joined_at > cutoff):
                skipped_recent += 1
                continue
            # Can't kick the owner, or anyone at/above the bot's highest role.
            if member.id == guild.owner_id or member.top_role >= guild.me.top_role:
                skipped_hierarchy += 1
                continue
            kickable.append(member)

        notes = []
        if skipped_recent:
            notes.append(f"{skipped_recent} joined too recently")
        if skipped_hierarchy:
            notes.append(f"{skipped_hierarchy} above me in the role list")
        if skipped_bots:
            notes.append(f"{skipped_bots} bots")
        note_line = f"\n\nSkipped: {', '.join(notes)}." if notes else ""

        if not kickable:
            await interaction.followup.send(
                f"Nobody to kick — no eligible members have the **{UNVERIFIED_ROLE_NAME}** "
                f"role.{note_line}"
            )
            return

        window = f" who joined {joined_days_ago}+ days ago" if joined_days_ago else ""
        preview_names = ", ".join(m.display_name for m in kickable[:15])
        if len(kickable) > 15:
            preview_names += f", …and {len(kickable) - 15} more"

        if preview:
            await interaction.followup.send(
                f"**Preview only — nobody was kicked.**\n"
                f"**{len(kickable)}** member(s) with **{UNVERIFIED_ROLE_NAME}**{window} "
                f"would be kicked:\n{preview_names}{note_line}"
            )
            return

        confirm_embed = discord.Embed(
            title="⚠️ Confirm prune",
            description=(
                f"This will kick **{len(kickable)}** member(s) holding the "
                f"**{UNVERIFIED_ROLE_NAME}** role{window}.\n\n{preview_names}{note_line}\n\n"
                "They can rejoin with a new invite. This cannot be undone otherwise."
            ),
            color=EMBED_RED,
        )
        view = ConfirmView(interaction.user.id, "Kick them", "Kicking…")
        await interaction.followup.send(embed=confirm_embed, view=view)

        if await view.wait():
            await interaction.followup.send("Timed out — nobody was kicked.", ephemeral=True)
            return
        if not view.confirmed:
            await interaction.followup.send("Cancelled — nobody was kicked.", ephemeral=True)
            return

        reason = f"Prune: {UNVERIFIED_ROLE_NAME} role, by {interaction.user} ({interaction.user.id})"
        kicked = 0
        failed = 0
        for index, member in enumerate(kickable):
            try:
                await member.kick(reason=reason)
                kicked += 1
            except discord.HTTPException as error:
                failed += 1
                logger.warning("Failed to kick %s (%s): %s", member, member.id, error)
            # Progress ping every 25 so a long prune doesn't look frozen.
            if kicked and kicked % 25 == 0:
                await interaction.followup.send(
                    f"…{kicked}/{len(kickable)} kicked so far.", ephemeral=True
                )
            if index < len(kickable) - 1:
                await asyncio.sleep(PRUNE_KICK_DELAY)

        result = discord.Embed(
            title="🧹 Prune complete",
            description=f"Kicked **{kicked}** member(s) with the **{UNVERIFIED_ROLE_NAME}** role."
            + (f"\n⚠️ **{failed}** could not be kicked (see bot logs)." if failed else ""),
            color=EMBED_COLOR if not failed else EMBED_RED,
        )
        await interaction.followup.send(embed=result)

    @lookup.autocomplete("cosmetic")
    async def lookup_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # Discord allows at most 25 autocomplete choices, 100 chars each.
        return [
            app_commands.Choice(name=m["display_name"][:100], value=m["display_name"][:100])
            for m in search_cosmetics(current, limit=25)
        ]

    # The items fields take a comma-separated list, so completion applies to the
    # segment after the last comma and keeps everything already typed.
    create_code_command.autocomplete("items")(_items_autocomplete)
    give_cosmetic_command.autocomplete("items")(_items_autocomplete)
    remove_cosmetic_command.autocomplete("items")(_items_autocomplete)

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


async def _items_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Completes the cosmetic being typed after the last comma, leaving the
    items already listed in front of it untouched."""
    prefix, separator, tail = current.rpartition(",")
    lead = f"{prefix}{separator} " if separator else ""

    tail = tail.strip()
    # Right after a comma there's nothing to search on yet, so show the top of
    # the catalog rather than an empty "no options match" list.
    matches = search_cosmetics(tail, limit=25) if tail else COSMETICS[:25]

    choices: list[app_commands.Choice[str]] = []
    for match in matches:
        value = f"{lead}{match['display_name']}"
        # Discord rejects a choice value over 100 chars, so drop suggestions
        # that would overflow rather than sending a truncated item list.
        if len(value) > 100:
            continue
        choices.append(app_commands.Choice(name=value[:100], value=value))
    return choices


async def _ensure_service_key(interaction: discord.Interaction) -> bool:
    """The supa admin tables are RLS-locked with no policies, so every admin
    command needs the service role key; fail with a setup hint instead of a
    Postgres permission error."""
    if SUPABASE_SERVICE_ROLE_KEY:
        return True
    await interaction.followup.send(
        "`SUPABASE_SERVICE_ROLE_KEY` is not set on the bot, so it can't touch "
        "these tables. Add it to the environment and restart."
    )
    return False


def _item_label(item_id: str) -> str:
    """An item id plus its catalog display name when we know it."""
    name = display_name_for(item_id)
    return f"`{item_id}` ({name})" if name else f"`{item_id}`"


def _joined_lines(lines: list[str], limit: int = 3900) -> str:
    """Joins lines for an embed description, cutting off with '+N more' safely
    under Discord's 4096-char description limit."""
    shown: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        cost = len(line) + (1 if shown else 0)
        if used + cost > limit:
            shown.append(f"… +{len(lines) - index} more")
            break
        shown.append(line)
        used += cost
    return "\n".join(shown)


def _ban_expiry(row: dict) -> datetime | None:
    """When a banned_players row runs out, or None if it never does.

    An unparseable banned_until is treated as permanent rather than as "already
    expired", so a bad timestamp can't make a live ban look finished.
    """
    if row.get("is_permanent") or not row.get("banned_until"):
        return None
    try:
        return datetime.fromisoformat(str(row["banned_until"]).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_remaining(delta: timedelta) -> str:
    """A timedelta as the two largest units that matter, e.g. '6d 4h', '12m'."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    # Round up to the next whole minute, so 44m59s reads as 45m rather than 44m.
    seconds = (seconds + 59) // 60 * 60
    parts = [
        ("d", seconds // 86400),
        ("h", seconds % 86400 // 3600),
        ("m", seconds % 3600 // 60),
    ]
    shown = [f"{value}{unit}" for unit, value in parts if value]
    return " ".join(shown[:2])


def _format_ban(row: dict) -> str:
    """One banned_players row as a single line: who, why, and for how long."""
    user_id = row.get("user_id", "?")
    reason = str(row.get("reason") or "no reason recorded").strip()
    if row.get("is_permanent") or not row.get("banned_until"):
        window = "permanent"
    else:
        try:
            until = datetime.fromisoformat(str(row["banned_until"]).replace("Z", "+00:00"))
            unix = int(until.timestamp())
            if until <= datetime.now(timezone.utc):
                window = f"expired <t:{unix}:R>"
            else:
                window = f"until <t:{unix}:f>"
        except ValueError:
            window = f"until {row['banned_until']}"
    return f"`{user_id}` — {reason} ({window})"


# tag_leaderboard rows come from the game client, so like friendpresence the
# column names may drift; accept the spellings we might see.
_LEADERBOARD_NAME_KEYS = (
    "display_name", "displayName", "username", "user_name",
    "player_name", "playerName", "nickname", "name",
)
_LEADERBOARD_ID_KEYS = ("player_id", "playerId", "user_id", "id")

# friendpresence is written by the game client too, so /username reads it through
# the same spelling-tolerant lookup. No id columns here: falling back to the UUID
# would just echo what staff already typed.
_PRESENCE_NAME_KEYS = (
    "displayname", "displayName", "display_name", "username", "user_name",
    "playername", "playerName", "nickname", "name",
)
_PRESENCE_SEEN_KEYS = (
    "updatedat", "updatedAt", "updated_at", "last_seen", "lastSeen",
    "last_seen_at", "heartbeat_at",
)
_PRESENCE_ROOM_KEYS = ("roomid", "roomId", "room_id", "room", "roomcode", "roomCode")


def _first_row_value(row: dict, keys: tuple[str, ...]) -> str | None:
    """First key in `keys` this row actually has a value for, as a string."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _parse_timestamp(raw: object) -> datetime | None:
    """Supabase ISO 8601 -> datetime, or None if the column is empty or unreadable."""
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _leaderboard_line(rank: int, row: dict) -> str:
    badge = RANK_BADGES[rank - 1] if rank <= len(RANK_BADGES) else f"`#{rank}`"
    who = None
    for key in _LEADERBOARD_NAME_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            who = f"**{_clean_room_name(str(value))}**"
            break
    if who is None:
        for key in _LEADERBOARD_ID_KEYS:
            value = row.get(key)
            if value not in (None, ""):
                who = f"`{value}`"
                break
    tags = row.get("total_tags") or 0
    return f"{badge} {who or 'Unknown'} — {int(tags):,} tags"


def _room_private_code(room: dict) -> str | None:
    """Pulls the private join code out of a room row, if the RPC returned one.

    get_active_rooms has used a few different key spellings for this, so we accept
    any of them rather than hard-coding one.
    """
    for key in ("privateCode", "privCode", "private_code", "roomCode", "room_code", "joinCode"):
        value = room.get(key)
        if value not in (None, "", 0):
            return str(value).strip()
    return None


def _clean_room_name(name: str) -> str:
    """Strip Unity rich-text markup (e.g. <size=1000%>😎</size> -> 😎) so room
    names render cleanly inside Discord instead of showing raw tags."""
    cleaned = re.sub(r"<[^>]+>", "", name or "").strip()
    return cleaned or (name or "Unknown")


def _players_line(names: list[str]) -> str:
    """One indented line naming everyone in a room, trimmed to a sane length."""
    cleaned = [_clean_room_name(name) for name in names]
    shown = cleaned[:MAX_PLAYER_NAMES_PER_ROOM]
    text = ", ".join(shown)
    remaining = len(cleaned) - len(shown)
    if remaining > 0:
        text += f" +{remaining} more"
    return f"　└ 👤 {text}"


def build_cosmetic_embed(item: dict) -> tuple[discord.Embed, list[discord.File]]:
    """Renders a cosmetic as the in-game store card: sprite on top, then an
    Overview block of Name / ID / Category / Cost.

    The sprite is uploaded alongside the message rather than linked, so the bot
    works from a fresh clone without the repo needing to be public.
    """
    embed = discord.Embed(title=item["display_name"], color=EMBED_COSMETIC)

    files: list[discord.File] = []
    sprite = image_path(item)
    if sprite:
        # Discord matches attachment:// against the filename it was uploaded as.
        files.append(discord.File(sprite, filename=sprite.name))
        embed.set_image(url=f"attachment://{sprite.name}")

    embed.add_field(name="Name", value=item["display_name"], inline=False)
    # Code block so Discord shows a copy button next to the ID.
    embed.add_field(name="ID", value=f"```\n{item['item_id']}\n```", inline=False)
    if item["category"]:
        embed.add_field(name="Category", value=item["category"], inline=True)
    cost = item["cost"]
    embed.add_field(
        name="Cost",
        value=f"{cost:,} {CURRENCY_NAME}" if cost else "Not purchasable",
        inline=True,
    )
    if item["bundled_items"]:
        embed.add_field(
            name="Bundled items",
            value=" ".join(f"`{item_id}`" for item_id in item["bundled_items"]),
            inline=False,
        )
    return embed, files


def build_rooms_embed(
    rooms: list[dict], players_by_room: dict[str, list[str]] | None = None
) -> discord.Embed:
    """Lists every active room (most populated first) with rank badges and the
    players inside each one, split across fields so we never blow past Discord's
    1024-char-per-field limit."""
    if not rooms:
        return discord.Embed(
            title="💤 No active rooms right now",
            description="Nobody has sent a room heartbeat in the last couple of minutes.",
            color=EMBED_COLOR,
        )

    total_players = sum(int(room.get("playerCount") or 0) for room in rooms)
    embed = discord.Embed(
        title=f"🎮 Active rooms ({len(rooms)})",
        description=f"👥 **{total_players}** player{'s' if total_players != 1 else ''} online across all rooms",
        color=EMBED_COLOR,
    )

    players_by_room = players_by_room or {}
    entries = []
    for rank, room in enumerate(rooms, start=1):
        badge = RANK_BADGES[rank - 1] if rank <= len(RANK_BADGES) else f"`#{rank}`"
        room_id = str(room.get("roomId") or "Unknown")
        name = _clean_room_name(room_id)
        players = int(room.get("playerCount") or 0)
        region = room.get("region") or "Unknown"
        zone = room.get("zone") or "Unknown"
        entry = f"{badge} **{name}** — 👥 {players} · 📍 {region} / {zone}"
        private_code = _room_private_code(room)
        if private_code:
            entry += f" · 🔒 `{private_code}`"
        elif room.get("isPrivate"):
            # Private, but the RPC didn't hand back a code for it.
            entry += " · 🔒 private"

        # Names come from friendpresence keyed by room id; a private room is also
        # looked up by its join code in case presence stores the code instead.
        names = players_by_room.get(normalize_room_key(room_id))
        if not names and private_code:
            names = players_by_room.get(normalize_room_key(private_code))
        if names:
            entry += "\n" + _players_line(names)
        entries.append(entry[:EMBED_FIELD_CHAR_LIMIT])

    # Pack as many room entries as fit into each 1024-char field, then start a new
    # field. An entry is a room and its player list, kept together.
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for entry in entries:
        # +1 accounts for the "\n" that joins entries together.
        entry_len = len(entry) + (1 if current else 0)
        if current and current_len + entry_len > EMBED_FIELD_CHAR_LIMIT:
            chunks.append(current)
            current = []
            current_len = 0
            entry_len = len(entry)  # no leading newline on the first entry of a chunk
        current.append(entry)
        current_len += entry_len

    if current:
        chunks.append(current)

    # Listing every player makes each room entry roughly twice as tall, so cap the
    # fields: Discord rejects an embed over 6000 characters outright.
    shown_chunks = chunks[:MAX_EMBED_FIELDS]
    hidden_rooms = sum(len(chunk) for chunk in chunks[len(shown_chunks):])

    for index, chunk in enumerate(shown_chunks):
        embed.add_field(
            name="🏠 Rooms" if index == 0 else f"🏠 Rooms (continued, {index + 1})",
            value="\n".join(chunk),
            inline=False,
        )

    if hidden_rooms:
        embed.set_footer(text=f"+{hidden_rooms} more room(s) not shown — too long for one message.")

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
