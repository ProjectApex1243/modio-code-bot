import os
import asyncio
import discord
from discord import app_commands
from aiohttp import web
from supabase import acreate_client, AsyncClient

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
LOG_CHANNEL_ID = int(os.environ["LOG_CHANNEL_ID"])
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
supabase: AsyncClient | None = None


@tree.command(
    name="code",
    description="Fetch the latest mod.io login code for an email",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(email="vstump_<id>@yourdomain")
async def code(interaction: discord.Interaction, email: str):
    res = (
        await supabase.table("modio_login_codes")
        .select("code, created_at")
        .eq("email", email)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data
    if rows:
        await interaction.response.send_message(
            f"Code for **{email}**: `{rows[0]['code']}`", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"No code found for **{email}**.", ephemeral=True
        )


async def handle_insert(payload):
    data = payload.get("data", payload)
    record = data.get("record") or data.get("new") or {}
    channel = client.get_channel(LOG_CHANNEL_ID) or await client.fetch_channel(LOG_CHANNEL_ID)
    embed = discord.Embed(title="New mod.io code")
    embed.add_field(name="Email", value=f"`{record.get('email', '?')}`", inline=False)
    embed.add_field(name="Code", value=f"`{record.get('code', '?')}`", inline=False)
    await channel.send(embed=embed)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    realtime = supabase.channel("modio_codes")
    await realtime.on_postgres_changes(
        "INSERT", schema="public", table="modio_login_codes", callback=handle_insert
    ).subscribe()


# Keep-alive HTTP server so Render's free Web Service stays bound to a port.
# Not needed if you deploy as a Background Worker.
async def start_keepalive():
    port = os.environ.get("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


async def main():
    global supabase
    supabase = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
    await start_keepalive()
    async with client:
        await client.start(DISCORD_TOKEN)


asyncio.run(main())
