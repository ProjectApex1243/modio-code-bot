"""Ticket system: one panel button that opens a three-option help menu.

  • Discord cosmetics — fully automatic. Mints a personal single-use redemption
    code and hands it over on the spot. Limited to one per Discord account,
    forever, enforced by the primary key on discord_ticket_claims.
  • Ban appeal      — opens a private channel and pings staff.
  • Something else  — same, with a different label.

Every button carries a fixed custom_id and every view is built with
timeout=None, so the panel keeps working across bot restarts as long as
register_persistent_views() runs at startup.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord

import supa_admin
from redeem_codes import create_code, generate_code

logger = logging.getLogger("room-bot")

# Cosmetics handed out by the Discord-cosmetics option. These ids were checked
# against the live title_data catalog — note the real id is "Discord Claim
# thing", not "Discord Claim", which silently grants nothing.
DEFAULT_COSMETIC_ITEMS = ["DiscordStick", "Discord Badge", "Discord Claim thing"]

# Exactly what staff would type by hand:
#   /create-code items:DiscordStick,Discord Badge,Discord Claim thing
#                duration:1d max_uses:1
CLAIM_CODE_MAX_USES = 1
CLAIM_CODE_EXPIRY = timedelta(days=1)

STAFF_REPLY_NOTICE = (
    "A staff member will come to help you whenever they can — please be "
    "patient. Leave any extra details here in the meantime."
)

REDEEM_INSTRUCTIONS = (
    "Now that you have your code, go in game and head to the **computer** in "
    "Stump (or anywhere else you find one). Go down to the **REDEEM** tab, "
    "type your code in and hit enter — and you're good!"
)
# generate_code() draws from 36^8 possibilities, so a collision is vanishingly
# rare — but it costs nothing to try again rather than fail the user's claim.
CODE_MINT_ATTEMPTS = 5

# Same default and same env var as main.py, so the two can't drift apart.
STAFF_ROLE_NAME = os.environ.get("STAFF_ROLE_NAME", "Staff")
EMBED_COLOR = 0x57F287
EMBED_RED = 0xED4245
EMBED_BLURPLE = 0x5865F2

# Ticket channels record who opened them in the channel topic, which is what
# stops one person from opening five appeals in a row.
TOPIC_PREFIX = "Ticket"
_TOPIC_RE = re.compile(rf"{TOPIC_PREFIX} • (?P<kind>[a-z]+) • (?P<user_id>\d+)")

# Each kind can land in its own category. The env var wins if it is set, so a
# category can be moved without a code change; default_category_id is the
# built-in fallback, and TICKET_CATEGORY_ID still covers both kinds at once.
TICKET_KINDS = {
    "appeal": {
        "label": "Ban appeal",
        "emoji": "⚖️",
        "channel_prefix": "appeal",
        "blurb": "Appeal a ban. A staff member will review it with you here.",
        "intro": (
            "Tell us your **in-game name**, your **player UUID** if you know "
            "it, and why you think the ban should be lifted."
        ),
        "color": EMBED_RED,
        "category_env": "TICKET_CATEGORY_ID_APPEAL",
        "default_category_id": 1470654959069696140,
    },
    "recover": {
        "label": "Get your cosmetics back",
        "emoji": "🎒",
        "channel_prefix": "recover",
        "blurb": (
            "Moved to the new app and your cosmetics are gone? Staff will put "
            "your old account back on your new one."
        ),
        # Everything staff needs is in this list, so the usual four rounds of
        # "what's your name again?" don't happen.
        "intro": (
            "Your cosmetics aren't lost — they're still on your old account. "
            "Meta gives every app a different id for the same person, so the "
            "new app doesn't recognise you yet. Staff can link the two.\n\n"
            "**Please post all of these:**\n"
            "1. Your **old in-game name**, spelled exactly as it was\n"
            "2. **Three cosmetics** you know you owned\n"
            "3. Roughly how many **shiny rocks** you had\n"
            "4. A screenshot of your old account if you have one\n\n"
            "If you ever donated, say so — donors are easy for us to confirm."
        ),
        "color": EMBED_BLURPLE,
        "category_env": "TICKET_CATEGORY_ID_RECOVER",
        # No built-in default: falls back to TICKET_CATEGORY_ID, then to
        # whatever category the panel itself lives in.
        "default_category_id": None,
    },
    "other": {
        "label": "Something else",
        "emoji": "💬",
        "channel_prefix": "help",
        "blurb": "Anything else — bugs, reports, questions.",
        "intro": "Describe what you need help with.",
        "color": EMBED_BLURPLE,
        "category_env": "TICKET_CATEGORY_ID_OTHER",
        "default_category_id": 1468743079371477093,
    },
}

# Populated by main.py at import time (main.py imports tickets, so this is
# the only direction that can't be a plain import) so a ticket kind can post
# extra staff controls right after opening - see open_staff_ticket below and
# main.py's _post_recovery_controls.
TICKET_OPENED_HOOKS: dict[str, Callable[[discord.TextChannel], Awaitable[None]]] = {}


def cosmetic_items() -> list[str]:
    """Reward items, overridable with TICKET_COSMETIC_ITEMS (comma-separated)."""
    raw = os.environ.get("TICKET_COSMETIC_ITEMS", "")
    items = [token.strip() for token in raw.split(",") if token.strip()]
    return items or list(DEFAULT_COSMETIC_ITEMS)


def _supabase_config() -> tuple[str | None, str | None]:
    return os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def _category_id_for(kind: str) -> int | None:
    """Which category a ticket of this kind belongs in.

    Order: the kind's own env var, then the built-in default for that kind,
    then the shared TICKET_CATEGORY_ID.

    The per-kind default deliberately outranks TICKET_CATEGORY_ID. Ban appeals
    are pinned to their own category, and a shared setting meant for general
    tickets must not silently drag them somewhere else; overriding them takes
    the explicit TICKET_CATEGORY_ID_APPEAL.
    """
    spec = TICKET_KINDS[kind]

    raw = os.environ.get(spec["category_env"], "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning(
                "%s is not a number: %r — ignoring it.", spec["category_env"], raw
            )

    if spec.get("default_category_id") is not None:
        return spec["default_category_id"]

    raw = os.environ.get("TICKET_CATEGORY_ID", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("TICKET_CATEGORY_ID is not a number: %r — ignoring it.", raw)
    return None


# Discord's hard cap on channels in one category. Not configurable, and the
# 51st is refused with a 400 that no amount of waiting clears.
MAX_CHANNELS_PER_CATEGORY = 50


def _overflow_index(name: str, base: str) -> int:
    """0 for the original category, N for an "<base> N" overflow."""
    match = re.fullmatch(rf"{re.escape(base)}\s+(\d+)", name)
    return int(match.group(1)) if match else 0


async def _category_with_room(
    guild: discord.Guild, category: discord.CategoryChannel | None, reason: str
) -> discord.CategoryChannel | None:
    """`category`, or an overflow beside it once it holds 50 channels.

    A full ticket category used to end the flow on a raw "Maximum number of
    channels in category reached (50)", reported to the player as "try again
    in a moment" — advice that could never work, because nothing frees a slot
    except staff closing tickets.

    Overflows are named "<original> 2", "3", ... and copy the original's
    permission overwrites, so a ticket that lands in one is exactly as private
    as it would have been. Returning None puts the channel at the guild root;
    it still carries its own overwrites, so it is private either way, and a
    ticket in an ugly place beats no ticket at all.
    """
    if category is None or len(category.channels) < MAX_CHANNELS_PER_CATEGORY:
        return category

    base = re.sub(r"\s+\d+$", "", category.name)
    siblings = [
        existing for existing in guild.categories
        if existing.name == base
        or re.fullmatch(rf"{re.escape(base)}\s+\d+", existing.name)
    ]
    for sibling in sorted(siblings, key=lambda c: _overflow_index(c.name, base)):
        if len(sibling.channels) < MAX_CHANNELS_PER_CATEGORY:
            return sibling

    # The original counts as index 0, so the first overflow is 2, not 1.
    next_index = max(2, max(_overflow_index(c.name, base) for c in siblings) + 1)
    try:
        created = await guild.create_category(
            name=f"{base} {next_index}",
            overwrites=dict(category.overwrites),
            reason=reason,
        )
    except discord.HTTPException:
        logger.exception("Could not add an overflow category next to %s", category.name)
        return None

    logger.info(
        "%s was full (%d channels); overflowed into %s",
        category.name, MAX_CHANNELS_PER_CATEGORY, created.name,
    )
    return created


def help_menu_embed() -> discord.Embed:
    """The 'what do you need help with?' screen, shown on the panel itself.

    The three options live directly on this message rather than behind a
    second, ephemeral menu. That is deliberate: discord.py rewrites
    timeout=None to 15 minutes for any view sent ephemerally, and when that
    copy expires it deletes its custom_ids from the store's shared dispatch
    table — which silently kills the buttons for everyone, permanently.
    Buttons on this ordinary message never time out, so they never do that.
    """
    embed = discord.Embed(
        title="What do you need help with?",
        description="Pick the option that fits best. Only you will see the reply.",
        color=EMBED_BLURPLE,
    )
    embed.add_field(
        name="🎁 Discord cosmetics",
        value=(
            "Get your free Discord cosmetics right now — no waiting.\n"
            "**One per person, so it can only be claimed once.**"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{TICKET_KINDS['recover']['emoji']} {TICKET_KINDS['recover']['label']}",
        value=(
            TICKET_KINDS["recover"]["blurb"]
            + "\n**Nothing was deleted** — your old account is still there.\n"
            "Opens a private channel with staff."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{TICKET_KINDS['appeal']['emoji']} {TICKET_KINDS['appeal']['label']}",
        value=TICKET_KINDS["appeal"]["blurb"] + "\nOpens a private channel with staff.",
        inline=False,
    )
    embed.add_field(
        name=f"{TICKET_KINDS['other']['emoji']} {TICKET_KINDS['other']['label']}",
        value=TICKET_KINDS["other"]["blurb"] + "\nOpens a private channel with staff.",
        inline=False,
    )
    return embed


def panel_embed() -> discord.Embed:
    """What /ticket-panel posts. Same screen as the help menu, since the
    options sit on the panel itself."""
    return help_menu_embed()


# --- Discord cosmetics -------------------------------------------------------


async def _mint_code(session, supabase_url: str, key: str) -> str:
    """Creates one unused single-use redemption code and returns it."""
    last_error: Exception | None = None
    for _attempt in range(CODE_MINT_ATTEMPTS):
        code = generate_code()
        try:
            await create_code(
                session, supabase_url, key, code, cosmetic_items(),
                CLAIM_CODE_MAX_USES, CLAIM_CODE_EXPIRY,
            )
            return code
        except RuntimeError as error:
            # Only a collision with an existing code is worth retrying.
            if "already exists" not in str(error):
                raise
            last_error = error
    raise RuntimeError(f"Could not find an unused code: {last_error}")


def _claimed_code_embed(code: str, items: list[str], *, fresh: bool) -> discord.Embed:
    embed = discord.Embed(
        title="🎁 Your Discord cosmetics" if fresh else "🎁 You already claimed these",
        color=EMBED_COLOR,
    )
    # Code block so Discord shows a copy button next to the code.
    embed.add_field(name="Your code", value=f"```\n{code}\n```", inline=False)
    embed.add_field(name="Instructions", value=REDEEM_INSTRUCTIONS, inline=False)
    embed.add_field(
        name="What you get", value="\n".join(f"`{item}`" for item in items), inline=False
    )
    embed.set_footer(
        text="Works once, and it's yours — don't share it. Expires in 24 hours."
    )
    return embed


async def handle_cosmetics_claim(interaction: discord.Interaction) -> None:
    """Mints and hands over the one-per-person cosmetics code."""
    await interaction.response.defer(ephemeral=True)
    supabase_url, key = _supabase_config()
    if not (supabase_url and key):
        await interaction.followup.send(
            "The cosmetics claim isn't set up yet — the bot is missing its "
            "Supabase service key. Ping a staff member.",
            ephemeral=True,
        )
        return

    session = interaction.client.http_session
    discord_id = str(interaction.user.id)
    items = cosmetic_items()

    # Stake the claim before minting anything: the primary key makes this the
    # atomic step, so a double-click can't produce two codes.
    try:
        staked = await supa_admin.stake_ticket_claim(
            session, supabase_url, key, discord_id, str(interaction.user)
        )
    except Exception as error:
        # Not just SupaError: a network blip raises an aiohttp error, and either
        # way nothing has been staked yet, so retrying is safe.
        logger.exception("Could not stake a cosmetics claim for %s", discord_id)
        # The reason goes in the reply too. It's ephemeral, so only the person
        # who pressed sees it, and it saves a trip through the hosting logs.
        await interaction.followup.send(
            "Something went wrong reaching the database. Nothing was used up — "
            "try again in a moment.\n\nIf it keeps happening, show staff this:\n"
            f"```\n{describe_error(error)}\n```",
            ephemeral=True,
        )
        return

    if not staked:
        existing = None
        try:
            existing = await supa_admin.fetch_ticket_claim(
                session, supabase_url, key, discord_id
            )
        except supa_admin.SupaError:
            logger.exception("Could not read the existing claim for %s", discord_id)

        if existing and existing.get("code"):
            embed = _claimed_code_embed(existing["code"], items, fresh=False)
            if existing.get("claimed_at"):
                try:
                    when = int(
                        datetime.fromisoformat(
                            str(existing["claimed_at"]).replace("Z", "+00:00")
                        ).timestamp()
                    )
                    # Codes only last a day, so say how old this one is — it may
                    # well have expired already.
                    embed.description = (
                        f"You claimed these <t:{when}:R>. If the code no longer "
                        "works, open a **Something else** ticket."
                    )
                except ValueError:
                    pass
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                "You've already claimed the Discord cosmetics, so there's nothing "
                "left to give. If you never got a code, open a **Something else** "
                "ticket and staff can sort it out.",
                ephemeral=True,
            )
        return

    try:
        code = await _mint_code(session, supabase_url, key)
    except Exception:
        logger.exception("Minting a cosmetics code failed for %s", discord_id)
        # Hand the claim back so a database hiccup doesn't cost them the reward.
        try:
            await supa_admin.release_ticket_claim(session, supabase_url, key, discord_id)
        except supa_admin.SupaError:
            logger.exception("Could not release the staked claim for %s", discord_id)
        await interaction.followup.send(
            "Couldn't create your code just now — nothing was used up, so press "
            "the button again in a moment.",
            ephemeral=True,
        )
        return

    try:
        await supa_admin.attach_ticket_claim_code(
            session, supabase_url, key, discord_id, code
        )
    except supa_admin.SupaError:
        # The code is already live and theirs; only our record of it is missing.
        logger.exception("Could not record code %s against claim %s", code, discord_id)

    await interaction.followup.send(
        embed=_claimed_code_embed(code, items, fresh=True), ephemeral=True
    )
    logger.info("Issued cosmetics code %s to %s (%s)", code, interaction.user, discord_id)


# --- Staff tickets -----------------------------------------------------------


def _channel_topic(kind: str, user_id: int) -> str:
    return f"{TOPIC_PREFIX} • {kind} • {user_id}"


def find_open_ticket(
    guild: discord.Guild, user_id: int, kind: str
) -> discord.TextChannel | None:
    """An existing open ticket of this kind for this user, if there is one."""
    for channel in guild.text_channels:
        match = _TOPIC_RE.search(channel.topic or "")
        if match and match["kind"] == kind and int(match["user_id"]) == user_id:
            return channel
    return None


def _channel_name(kind: str, user: discord.abc.User) -> str:
    prefix = TICKET_KINDS[kind]["channel_prefix"]
    # Discord lowercases names and replaces awkward characters anyway; doing it
    # here keeps the result predictable instead of letting it mangle the name.
    slug = re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-") or "player"
    return f"{prefix}-{slug}"[:100]


async def open_staff_ticket(interaction: discord.Interaction, kind: str) -> None:
    """Creates the private channel for a ban appeal or a general question."""
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(
            "Tickets only work inside the server.", ephemeral=True
        )
        return

    existing = find_open_ticket(guild, interaction.user.id, kind)
    if existing is not None:
        await interaction.followup.send(
            f"You already have a **{TICKET_KINDS[kind]['label']}** ticket open: "
            f"{existing.mention}",
            ephemeral=True,
        )
        return

    if not guild.me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "I can't open a ticket — I'm missing the **Manage Channels** "
            "permission. Please tell a staff member.",
            ephemeral=True,
        )
        return

    category = None
    category_id = _category_id_for(kind)
    if category_id is not None:
        found = guild.get_channel(category_id)
        if isinstance(found, discord.CategoryChannel):
            category = found
        else:
            logger.warning(
                "Category %s for %s tickets is missing or is not a category "
                "(is the bot in that server, and can it see it?). Falling back.",
                category_id, kind,
            )
    if category is None and isinstance(interaction.channel, discord.TextChannel):
        # Fall back to wherever the panel lives, so tickets stay near it.
        category = interaction.channel.category

    category = await _category_with_room(
        guild, category, reason=f"{TICKET_KINDS[kind]['label']} tickets overflowed"
    )

    staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_channels=True, read_message_history=True,
        ),
    }
    if staff_role is not None:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, read_message_history=True,
        )

    try:
        channel = await guild.create_text_channel(
            name=_channel_name(kind, interaction.user),
            category=category,
            overwrites=overwrites,
            topic=_channel_topic(kind, interaction.user.id),
            reason=f"{TICKET_KINDS[kind]['label']} ticket for {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I'm not allowed to create a channel there. A staff member needs to "
            "check my permissions on the ticket category.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        logger.exception("Creating a %s ticket failed for %s", kind, interaction.user)
        # Deliberately not "try again in a moment": the usual cause is the
        # server being out of channel room, which only staff can clear.
        await interaction.followup.send(
            "Couldn't open your ticket. The server may be out of room for new "
            "channels, which staff have to clear — retrying won't fix that.\n"
            "Please show a staff member this:\n"
            f"```\n{describe_error(error)}\n```",
            ephemeral=True,
        )
        return

    spec = TICKET_KINDS[kind]
    embed = discord.Embed(
        title=f"{spec['emoji']} {spec['label']}",
        description=(
            f"{interaction.user.mention} opened this ticket.\n\n" + spec["intro"]
        ),
        color=spec.get("color", EMBED_BLURPLE),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="What happens next", value=STAFF_REPLY_NOTICE, inline=False)
    embed.set_footer(text=f"Opened by {interaction.user} • {interaction.user.id}")

    # Deliberately no role ping: staff already see the channel through the
    # overwrite above, and the embed tells the player someone will come.
    # Mentions are suppressed so nothing in here can notify anyone.
    await channel.send(
        embed=embed,
        view=TicketCloseView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )

    hook = TICKET_OPENED_HOOKS.get(kind)
    if hook is not None:
        try:
            await hook(channel)
        except discord.HTTPException:
            logger.exception("Post-open hook for %s ticket failed", kind)

    await interaction.followup.send(
        f"Opened your ticket: {channel.mention}", ephemeral=True
    )
    logger.info("Opened %s ticket %s for %s", kind, channel.id, interaction.user)


async def close_ticket(interaction: discord.Interaction) -> None:
    """Deletes the ticket channel, once the presser is allowed to close it."""
    channel = interaction.channel
    match = _TOPIC_RE.search(getattr(channel, "topic", "") or "")
    if match is None:
        await interaction.response.send_message(
            "This doesn't look like a ticket channel.", ephemeral=True
        )
        return

    opener_id = int(match["user_id"])
    is_staff = any(role.name == STAFF_ROLE_NAME for role in getattr(interaction.user, "roles", []))
    if interaction.user.id != opener_id and not is_staff:
        await interaction.response.send_message(
            "Only the person who opened this ticket or a staff member can close it.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Close this ticket? The channel and everything in it will be deleted.",
        view=ConfirmCloseView(interaction.user.id),
        ephemeral=True,
    )


class ConfirmCloseView(discord.ui.View):
    """Second step of closing, so a stray click doesn't delete the history."""

    def __init__(self, invoker_id: int) -> None:
        super().__init__(timeout=60)
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    @discord.ui.button(label="Delete it", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Closing…", view=None)
        try:
            await interaction.channel.delete(
                reason=f"Ticket closed by {interaction.user}"
            )
        except discord.HTTPException:
            logger.exception("Could not delete ticket channel %s", interaction.channel)
            await interaction.followup.send(
                "I couldn't delete the channel — check my permissions.", ephemeral=True
            )

    @discord.ui.button(label="Keep it open", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Left open.", view=None)


# --- Persistent views --------------------------------------------------------


class _TicketView(discord.ui.View):
    """Shared base for the ticket views.

    discord.py's default on_error only writes to the bot's console, so a button
    that raises looks exactly like a button nobody wired up — the click just
    appears to do nothing. Reporting it to the person who pressed it turns a
    silent failure into something they can actually tell staff about.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        label = getattr(item, "custom_id", None) or type(item).__name__
        logger.exception("Ticket button %s failed", label, exc_info=error)
        message = (
            "Something broke on our end and this didn't go through — nothing was "
            "used up. Please tell a staff member."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not even report the failure for %s", label)


class TicketPanelView(_TicketView):
    """The support panel: all three options, always on, never expiring.

    Every reply these produce is ephemeral, but the buttons themselves live on
    an ordinary message so nothing ever removes them from the dispatch table.
    """

    @discord.ui.button(
        label="Discord cosmetics", emoji="🎁",
        style=discord.ButtonStyle.success, custom_id="ticket:cosmetics",
    )
    async def cosmetics(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await handle_cosmetics_claim(interaction)

    @discord.ui.button(
        label="Get your cosmetics back", emoji="🎒",
        style=discord.ButtonStyle.primary, custom_id="ticket:recover",
    )
    async def recover(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await open_staff_ticket(interaction, "recover")

    @discord.ui.button(
        label="Ban appeal", emoji="⚖️",
        style=discord.ButtonStyle.secondary, custom_id="ticket:appeal",
    )
    async def appeal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await open_staff_ticket(interaction, "appeal")

    @discord.ui.button(
        label="Something else", emoji="💬",
        style=discord.ButtonStyle.secondary, custom_id="ticket:other",
    )
    async def other(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await open_staff_ticket(interaction, "other")


class TicketCloseView(_TicketView):
    """Close button pinned to the top of every staff ticket channel."""

    @discord.ui.button(
        label="Close ticket", emoji="🔒",
        style=discord.ButtonStyle.danger, custom_id="ticket:close",
    )
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await close_ticket(interaction)


# Only views that live on ordinary (non-ephemeral) messages belong here. A view
# sent ephemerally must never be registered persistently — see help_menu_embed.
PERSISTENT_VIEWS = (TicketPanelView, TicketCloseView)


def registered_custom_ids() -> list[str]:
    """Every button id the bot answers to — logged at startup so a dead button
    can be told apart from one that was never registered."""
    return [
        str(child.custom_id)
        for view_cls in PERSISTENT_VIEWS
        for child in view_cls().children
        if getattr(child, "custom_id", None)
    ]


def describe_error(error: BaseException) -> str:
    """A short, safe one-liner for an exception.

    Never includes headers, so the service role key can't leak into a Discord
    message or a log line.
    """
    detail = str(error).strip() or error.__class__.__name__
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return f"{error.__class__.__name__}: {detail}"


async def self_test(session) -> None:
    """Checks at startup that the claims table is actually reachable.

    Without this the first failure anyone sees is a player pressing the button,
    which is a bad place to discover the bot can't talk to Supabase.
    """
    supabase_url, key = _supabase_config()
    if not supabase_url:
        logger.error("TICKET SELF-TEST: SUPABASE_URL is not set.")
        return
    if not key:
        logger.error(
            "TICKET SELF-TEST: SUPABASE_SERVICE_ROLE_KEY is not set — the "
            "cosmetics claim will refuse every press."
        )
        return
    if not supabase_url.startswith(("http://", "https://")):
        logger.error(
            "TICKET SELF-TEST: SUPABASE_URL is %r, which has no http(s):// "
            "scheme. Every request will fail before it is sent.", supabase_url
        )
        return
    if session is None:
        logger.error("TICKET SELF-TEST: the bot has no HTTP session.")
        return

    try:
        await supa_admin.fetch_ticket_claim(session, supabase_url, key, "0")
    except Exception as error:
        logger.error(
            "TICKET SELF-TEST FAILED against %s — the cosmetics claim will not "
            "work: %s", supabase_url, describe_error(error)
        )
        return
    logger.info("TICKET SELF-TEST OK: %s is reachable.", supabase_url)


def register_persistent_views(client: discord.Client) -> None:
    """Re-attaches the ticket buttons after a restart. Without this, every
    button posted before the restart stops responding."""
    for view_cls in PERSISTENT_VIEWS:
        client.add_view(view_cls())
