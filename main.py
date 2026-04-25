import discord
from discord.ext import commands, tasks
import aiosqlite
import sqlite3
import asyncio
import datetime
import tempfile
import random
import os
import threading
import urllib.parse
import requests as req_lib
from typing import Optional
from contextlib import contextmanager
from flask import Flask, render_template, redirect, request, session, jsonify

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

DB_PATH = "credentials.db"
DEFAULT_PREFIX = "/"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-please")

# ─────────────────────────────────────────────
# DATABASE — SYNC (Flask)
# ─────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db_sync():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT DEFAULT '/',
                welcome_channel INTEGER,
                welcome_message TEXT,
                leave_channel INTEGER,
                leave_message TEXT,
                log_channel INTEGER,
                autorole INTEGER
            );
            CREATE TABLE IF NOT EXISTS bypass_roles (
                guild_id INTEGER, role_id INTEGER,
                PRIMARY KEY (guild_id, role_id)
            );
            CREATE TABLE IF NOT EXISTS command_roles (
                guild_id INTEGER, command_name TEXT, role_id INTEGER,
                PRIMARY KEY (guild_id, command_name, role_id)
            );
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER, mod_id INTEGER,
                reason TEXT, timestamp TEXT
            );
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
                host_id INTEGER, prize TEXT, winners INTEGER,
                ends_at TEXT, ended INTEGER DEFAULT 0, rigged_user_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
                open INTEGER DEFAULT 1, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER, support_role_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS mutes (
                guild_id INTEGER, user_id INTEGER, expires_at TEXT,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS verification_settings (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER DEFAULT 0,
                unverified_role_id INTEGER,
                verified_role_id INTEGER,
                channel_id INTEGER,
                message TEXT DEFAULT 'Click the button below to verify your account.',
                min_account_age INTEGER DEFAULT 0,
                kick_bots INTEGER DEFAULT 1,
                dm_on_join INTEGER DEFAULT 1,
                log_channel_id INTEGER
            );
        """)

init_db_sync()

# ─────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = tempfile.gettempdir()
app.config["SESSION_PERMANENT"] = True
from flask_session import Session
Session(app)

def check_access(guild_id):
    MANAGE_GUILD = 0x20
    ADMIN = 0x8
    for g in session.get("guilds", []):
        if int(g["id"]) == int(guild_id):
            perms = int(g.get("permissions", 0))
            return bool(perms & ADMIN) or bool(perms & MANAGE_GUILD)
    return False

@app.route("/")
def index():
    return render_template("index.html",
                           client_id=CLIENT_ID,
                           logged_in="user" in session,
                           user=session.get("user"))

@app.route("/login")
def login():
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "prompt": "none"
    }
    return redirect(f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}")

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/?error=1")
    r = req_lib.post("https://discord.com/api/oauth2/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    })
    token_data = r.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return redirect("/?error=1")
    headers = {"Authorization": f"Bearer {access_token}"}
    user = req_lib.get("https://discord.com/api/users/@me", headers=headers).json()
    guilds = req_lib.get("https://discord.com/api/users/@me/guilds", headers=headers).json()
    session.permanent = True
    session["user"] = user
    session["guilds"] = guilds if isinstance(guilds, list) else []
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    MANAGE_GUILD = 0x20
    ADMIN = 0x8
    bot_guild_ids = {g.id for g in bot.guilds}
    mutual, other = [], []
    for g in session.get("guilds", []):
        perms = int(g.get("permissions", 0))
        if not (bool(perms & ADMIN) or bool(perms & MANAGE_GUILD)):
            continue
        g_copy = dict(g)
        g_copy["in_bot"] = int(g["id"]) in bot_guild_ids
        if g_copy["in_bot"]:
            mutual.append(g_copy)
        else:
            other.append(g_copy)
    return render_template("dashboard.html",
                           user=session["user"],
                           mutual=mutual,
                           other=other,
                           client_id=CLIENT_ID)

@app.route("/dashboard/<int:guild_id>")
def server_dashboard(guild_id):
    if "user" not in session:
        return redirect("/login")
    if not check_access(guild_id):
        return redirect("/dashboard")
    guild = bot.get_guild(guild_id)
    if not guild:
        return redirect("/dashboard")
    return render_template("server.html",
                           user=session["user"],
                           guild={
                               "id": str(guild.id),
                               "name": guild.name,
                               "icon": str(guild.icon) if guild.icon else None,
                               "member_count": guild.member_count
                           })

# ── API ────────────────────────────────────────

@app.route("/api/guild/<int:guild_id>")
def api_guild(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    guild = bot.get_guild(guild_id)
    if not guild:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": str(guild.id),
        "name": guild.name,
        "member_count": guild.member_count,
        "roles": [
            {"id": str(r.id), "name": r.name, "color": r.color.value}
            for r in sorted(guild.roles, key=lambda r: -r.position)
            if not r.is_default()
        ],
        "text_channels": [
            {"id": str(c.id), "name": c.name}
            for c in sorted(guild.text_channels, key=lambda c: c.position)
        ],
        "categories": [
            {"id": str(c.id), "name": c.name}
            for c in guild.categories
        ]
    })

@app.route("/api/guild/<int:guild_id>/settings")
def api_guild_settings(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    with get_db() as conn:
        gs = conn.execute("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)).fetchone()
        ts = conn.execute("SELECT * FROM ticket_settings WHERE guild_id=?", (guild_id,)).fetchone()
        vs = conn.execute("SELECT * FROM verification_settings WHERE guild_id=?", (guild_id,)).fetchone()
        bypass = [str(r[0]) for r in conn.execute("SELECT role_id FROM bypass_roles WHERE guild_id=?", (guild_id,)).fetchall()]
        cmdroles = {}
        for row in conn.execute("SELECT command_name, role_id FROM command_roles WHERE guild_id=?", (guild_id,)).fetchall():
            if row[0] not in cmdroles:
                cmdroles[row[0]] = []
            cmdroles[row[0]].append(str(row[1]))
    return jsonify({
        "general": {
            "prefix": gs["prefix"] if gs else "/",
            "log_channel": str(gs["log_channel"]) if gs and gs["log_channel"] else None,
            "autorole": str(gs["autorole"]) if gs and gs["autorole"] else None,
        },
        "welcome": {
            "welcome_channel": str(gs["welcome_channel"]) if gs and gs["welcome_channel"] else None,
            "welcome_message": gs["welcome_message"] if gs and gs["welcome_message"] else "",
            "leave_channel": str(gs["leave_channel"]) if gs and gs["leave_channel"] else None,
            "leave_message": gs["leave_message"] if gs and gs["leave_message"] else "",
        },
        "moderation": {
            "bypass_roles": bypass,
            "command_roles": cmdroles,
        },
        "verification": {
            "enabled": bool(vs["enabled"]) if vs else False,
            "unverified_role_id": str(vs["unverified_role_id"]) if vs and vs["unverified_role_id"] else None,
            "verified_role_id": str(vs["verified_role_id"]) if vs and vs["verified_role_id"] else None,
            "channel_id": str(vs["channel_id"]) if vs and vs["channel_id"] else None,
            "message": vs["message"] if vs else "Click the button below to verify your account.",
            "min_account_age": vs["min_account_age"] if vs else 0,
            "kick_bots": bool(vs["kick_bots"]) if vs else True,
            "dm_on_join": bool(vs["dm_on_join"]) if vs else True,
            "log_channel_id": str(vs["log_channel_id"]) if vs and vs["log_channel_id"] else None,
        },
        "tickets": {
            "category_id": str(ts["category_id"]) if ts and ts["category_id"] else None,
            "support_role_id": str(ts["support_role_id"]) if ts and ts["support_role_id"] else None,
        }
    })

@app.route("/api/guild/<int:guild_id>/general", methods=["POST"])
def api_save_general(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    d = request.json
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        conn.execute("UPDATE guild_settings SET prefix=?, log_channel=?, autorole=? WHERE guild_id=?",
                     (d.get("prefix", "/"),
                      int(d["log_channel"]) if d.get("log_channel") else None,
                      int(d["autorole"]) if d.get("autorole") else None,
                      guild_id))
    return jsonify({"ok": True})

@app.route("/api/guild/<int:guild_id>/welcome", methods=["POST"])
def api_save_welcome(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    d = request.json
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        conn.execute("""UPDATE guild_settings SET welcome_channel=?, welcome_message=?,
                        leave_channel=?, leave_message=? WHERE guild_id=?""",
                     (int(d["welcome_channel"]) if d.get("welcome_channel") else None,
                      d.get("welcome_message", ""),
                      int(d["leave_channel"]) if d.get("leave_channel") else None,
                      d.get("leave_message", ""),
                      guild_id))
    return jsonify({"ok": True})

@app.route("/api/guild/<int:guild_id>/moderation", methods=["POST"])
def api_save_moderation(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    d = request.json
    with get_db() as conn:
        conn.execute("DELETE FROM bypass_roles WHERE guild_id=?", (guild_id,))
        for rid in d.get("bypass_roles", []):
            conn.execute("INSERT OR IGNORE INTO bypass_roles (guild_id, role_id) VALUES (?,?)", (guild_id, int(rid)))
        conn.execute("DELETE FROM command_roles WHERE guild_id=?", (guild_id,))
        for cmd, roles in d.get("command_roles", {}).items():
            for rid in roles:
                conn.execute("INSERT OR IGNORE INTO command_roles (guild_id, command_name, role_id) VALUES (?,?,?)",
                             (guild_id, cmd, int(rid)))
    return jsonify({"ok": True})

@app.route("/api/guild/<int:guild_id>/verification", methods=["POST"])
def api_save_verification(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    d = request.json
    with get_db() as conn:
        conn.execute("""INSERT OR REPLACE INTO verification_settings
                        (guild_id, enabled, unverified_role_id, verified_role_id, channel_id,
                         message, min_account_age, kick_bots, dm_on_join, log_channel_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (guild_id,
                      1 if d.get("enabled") else 0,
                      int(d["unverified_role_id"]) if d.get("unverified_role_id") else None,
                      int(d["verified_role_id"]) if d.get("verified_role_id") else None,
                      int(d["channel_id"]) if d.get("channel_id") else None,
                      d.get("message", "Click the button below to verify your account."),
                      int(d.get("min_account_age", 0)),
                      1 if d.get("kick_bots") else 0,
                      1 if d.get("dm_on_join") else 0,
                      int(d["log_channel_id"]) if d.get("log_channel_id") else None))
    return jsonify({"ok": True})

@app.route("/api/guild/<int:guild_id>/tickets", methods=["POST"])
def api_save_tickets(guild_id):
    if "user" not in session or not check_access(guild_id):
        return jsonify({"error": "Forbidden"}), 403
    d = request.json
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO ticket_settings (guild_id, category_id, support_role_id) VALUES (?,?,?)",
                     (guild_id,
                      int(d["category_id"]) if d.get("category_id") else None,
                      int(d["support_role_id"]) if d.get("support_role_id") else None))
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────

async def get_prefix(bot, message):
    if not message.guild:
        return DEFAULT_PREFIX
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT prefix FROM guild_settings WHERE guild_id=?", (message.guild.id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else DEFAULT_PREFIX

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

# ─────────────────────────────────────────────
# BOT HELPERS
# ─────────────────────────────────────────────

async def get_setting(guild_id, column):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT {column} FROM guild_settings WHERE guild_id=?", (guild_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_setting(guild_id, column, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        await db.execute(f"UPDATE guild_settings SET {column}=? WHERE guild_id=?", (value, guild_id))
        await db.commit()

async def has_bypass(member):
    if member.guild_permissions.administrator:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role_id FROM bypass_roles WHERE guild_id=?", (member.guild.id,)) as cur:
            bypass_ids = {r[0] for r in await cur.fetchall()}
    return any(r.id in bypass_ids for r in member.roles)

async def can_use_command(member, command_name):
    if await has_bypass(member):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role_id FROM command_roles WHERE guild_id=? AND command_name=?",
                              (member.guild.id, command_name)) as cur:
            rows = await cur.fetchall()
    if not rows:
        return True
    required = {r[0] for r in rows}
    return any(r.id in required for r in member.roles)

async def log_action(guild, embed):
    log_id = await get_setting(guild.id, "log_channel")
    if log_id:
        ch = guild.get_channel(log_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

def mod_embed(title, color, **fields):
    embed = discord.Embed(title=title, color=color, timestamp=datetime.datetime.utcnow())
    for name, value in fields.items():
        embed.add_field(name=name.replace("_", " ").title(), value=str(value), inline=True)
    return embed

def parse_time(s):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(s[:-1]) * units[s[-1]]
    except Exception:
        return None

# ─────────────────────────────────────────────
# BOT EVENTS
# ─────────────────────────────────────────────

bot_start_time = datetime.datetime.utcnow()

@bot.event
async def on_ready():
    check_giveaways.start()
    check_mutes.start()
    bot.add_view(VerifyView())
    bot.add_view(TicketView())
    print(f"Online: {bot.user} | {len(bot.guilds)} servers")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="your server"))

@bot.event
async def on_guild_join(guild):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild.id,))
        await db.commit()

@bot.event
async def on_member_join(member):
    # Welcome message
    wc = await get_setting(member.guild.id, "welcome_channel")
    wm = await get_setting(member.guild.id, "welcome_message")
    if wc and wm:
        ch = member.guild.get_channel(wc)
        if ch:
            msg = wm.replace("{user}", member.mention).replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count))
            await ch.send(msg)
    # Autorole
    ar = await get_setting(member.guild.id, "autorole")
    if ar:
        role = member.guild.get_role(ar)
        if role:
            try:
                await member.add_roles(role)
            except Exception:
                pass
    # Verification
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT * FROM verification_settings WHERE guild_id=?", (member.guild.id,)) as cur:
            vs = await cur.fetchone()
    if vs and vs[1]:  # enabled
        if member.bot and vs[7]:  # kick_bots
            try:
                await member.kick(reason="Bot detected, verification enabled")
            except Exception:
                pass
            return
        unverified_role_id = vs[2]
        if unverified_role_id:
            role = member.guild.get_role(unverified_role_id)
            if role:
                try:
                    await member.add_roles(role)
                except Exception:
                    pass
        if vs[8]:  # dm_on_join
            channel_id = vs[4]
            if channel_id:
                ch = member.guild.get_channel(channel_id)
                if ch:
                    try:
                        await member.send(f"Welcome to **{member.guild.name}**! Please verify yourself in {ch.mention} to gain access.")
                    except Exception:
                        pass

@bot.event
async def on_member_remove(member):
    lc = await get_setting(member.guild.id, "leave_channel")
    lm = await get_setting(member.guild.id, "leave_message")
    if lc and lm:
        ch = member.guild.get_channel(lc)
        if ch:
            msg = lm.replace("{user}", str(member)).replace("{server}", member.guild.name)
            await ch.send(msg)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You do not have permission to use this command.")
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send(f"I am missing permissions: {', '.join(error.missing_permissions)}")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Invalid argument. Check the command usage.")
    else:
        await ctx.send(f"Error: {error}")

# ─────────────────────────────────────────────
# VERIFICATION SYSTEM
# ─────────────────────────────────────────────

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="credentials_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        guild = interaction.guild
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT * FROM verification_settings WHERE guild_id=?", (guild.id,)) as cur:
                vs = await cur.fetchone()
        if not vs or not vs[1]:
            return await interaction.response.send_message("Verification is not enabled.", ephemeral=True)

        # Min account age check
        min_age = vs[6]
        if min_age > 0:
            age = (datetime.datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
            if age < min_age:
                return await interaction.response.send_message(
                    f"Your account is too new. You need an account older than {min_age} days to verify.",
                    ephemeral=True
                )

        unverified_id = vs[2]
        verified_id = vs[3]
        removed = False
        if unverified_id:
            role = guild.get_role(unverified_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role)
                    removed = True
                except Exception:
                    pass
            elif role and role not in member.roles:
                return await interaction.response.send_message("You are already verified.", ephemeral=True)

        if verified_id:
            role = guild.get_role(verified_id)
            if role:
                try:
                    await member.add_roles(role)
                except Exception:
                    pass

        # Log
        log_ch_id = vs[9]
        if log_ch_id:
            ch = guild.get_channel(log_ch_id)
            if ch:
                embed = discord.Embed(
                    title="Member Verified",
                    color=discord.Color.green(),
                    timestamp=datetime.datetime.utcnow()
                )
                embed.add_field(name="User", value=f"{member} ({member.id})")
                embed.add_field(name="Account Age", value=f"{(datetime.datetime.utcnow() - member.created_at.replace(tzinfo=None)).days} days")
                await ch.send(embed=embed)

        await interaction.response.send_message("You have been verified. Welcome!", ephemeral=True)

@bot.group(name="verification", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def verification(ctx):
    await ctx.send("Use `verification setup`, `verification enable`, `verification disable`, `verification panel`, or configure via the dashboard.")

@verification.command(name="setup")
@commands.has_permissions(manage_guild=True)
async def verification_setup(ctx):
    guild = ctx.guild
    # Create Unverified role
    unverified_role = discord.utils.get(guild.roles, name="Unverified")
    if not unverified_role:
        unverified_role = await guild.create_role(name="Unverified", color=discord.Color.dark_grey())

    # Set channel perms: Unverified can only see verify channel
    for channel in guild.channels:
        if channel.name != "verify":
            try:
                await channel.set_permissions(unverified_role, read_messages=False)
            except Exception:
                pass

    # Create or find verify channel
    verify_channel = discord.utils.get(guild.text_channels, name="verify")
    if not verify_channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            unverified_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        verify_channel = await guild.create_text_channel("verify", overwrites=overwrites)

    # Save to DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT OR REPLACE INTO verification_settings
                           (guild_id, enabled, unverified_role_id, channel_id)
                           VALUES (?,1,?,?)""",
                         (guild.id, unverified_role.id, verify_channel.id))
        await db.commit()

    # Send panel
    embed = discord.Embed(
        title="Verification Required",
        description="Click the button below to verify your account and gain access to the server.",
        color=discord.Color.from_str("#010a17")
    )
    await verify_channel.send(embed=embed, view=VerifyView())
    await ctx.send(f"Verification set up. Unverified role: **{unverified_role.name}**, Channel: {verify_channel.mention}")

@verification.command(name="enable")
@commands.has_permissions(manage_guild=True)
async def verification_enable(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO verification_settings (guild_id) VALUES (?)", (ctx.guild.id,))
        await db.execute("UPDATE verification_settings SET enabled=1 WHERE guild_id=?", (ctx.guild.id,))
        await db.commit()
    await ctx.send("Verification enabled.")

@verification.command(name="disable")
@commands.has_permissions(manage_guild=True)
async def verification_disable(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE verification_settings SET enabled=0 WHERE guild_id=?", (ctx.guild.id,))
        await db.commit()
    await ctx.send("Verification disabled.")

@verification.command(name="panel")
@commands.has_permissions(manage_guild=True)
async def verification_panel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT message FROM verification_settings WHERE guild_id=?", (ctx.guild.id,)) as cur:
            row = await cur.fetchone()
    msg = row[0] if row else "Click the button below to verify your account."
    embed = discord.Embed(title="Verification Required", description=msg, color=discord.Color.from_str("#010a17"))
    await channel.send(embed=embed, view=VerifyView())
    await ctx.send(f"Verification panel sent to {channel.mention}.")

@verification.command(name="setrole")
@commands.has_permissions(manage_guild=True)
async def verification_setrole(ctx, kind: str, role: discord.Role):
    kind = kind.lower()
    if kind == "unverified":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO verification_settings (guild_id) VALUES (?)", (ctx.guild.id,))
            await db.execute("UPDATE verification_settings SET unverified_role_id=? WHERE guild_id=?", (role.id, ctx.guild.id))
            await db.commit()
        await ctx.send(f"Unverified role set to **{role.name}**.")
    elif kind == "verified":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO verification_settings (guild_id) VALUES (?)", (ctx.guild.id,))
            await db.execute("UPDATE verification_settings SET verified_role_id=? WHERE guild_id=?", (role.id, ctx.guild.id))
            await db.commit()
        await ctx.send(f"Verified role set to **{role.name}**.")
    else:
        await ctx.send("Kind must be `unverified` or `verified`.")

@verification.command(name="minage")
@commands.has_permissions(manage_guild=True)
async def verification_minage(ctx, days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO verification_settings (guild_id) VALUES (?)", (ctx.guild.id,))
        await db.execute("UPDATE verification_settings SET min_account_age=? WHERE guild_id=?", (days, ctx.guild.id))
        await db.commit()
    if days == 0:
        await ctx.send("Minimum account age requirement removed.")
    else:
        await ctx.send(f"Accounts must be at least {days} days old to verify.")

# ─────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx, category: str = None):
    prefix = await get_prefix(bot, ctx.message)
    categories = {
        "moderation": ["ban", "unban", "kick", "timeout", "untimeout", "warn", "warnings", "clearwarnings", "mute", "unmute", "purge", "lock", "unlock", "slowmode", "nick"],
        "utility": ["ping", "prefix", "serverinfo", "userinfo", "avatar", "roleinfo", "invite", "uptime", "botinfo"],
        "roles": ["role add/remove", "autorole", "bypass add/remove/list", "cmdrole set/clear"],
        "verification": ["verification setup", "verification enable/disable", "verification panel", "verification setrole", "verification minage"],
        "giveaway": ["giveaway start", "giveaway end", "giveaway reroll"],
        "tickets": ["ticket setup", "ticket panel", "ticket close", "ticket add", "ticket remove"],
        "welcome": ["welcome channel", "welcome message", "leave channel", "leave message", "logging"],
        "fun": ["8ball", "coinflip", "roll", "rps", "choose", "joke", "fact", "roast", "compliment", "ship", "rate", "mock"]
    }
    if category and category.lower() in categories:
        embed = discord.Embed(title=f"{category.title()} Commands", color=discord.Color.from_str("#010a17"))
        embed.description = "\n".join(f"`{prefix}{c}`" for c in categories[category.lower()])
        embed.set_footer(text=f"Prefix: {prefix}")
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(title="Credentials — Help", color=discord.Color.from_str("#010a17"))
        embed.description = f"Use `{prefix}help <category>` for detailed commands.\nDashboard: configure everything at your web panel."
        for cat in categories:
            embed.add_field(name=cat.title(), value=f"`{prefix}help {cat}`", inline=True)
        await ctx.send(embed=embed)

# ─────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────

@bot.command(name="ping")
async def ping(ctx):
    embed = discord.Embed(title="Pong!", color=discord.Color.from_str("#010a17"))
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms")
    await ctx.send(embed=embed)

@bot.command(name="prefix")
@commands.has_permissions(manage_guild=True)
async def change_prefix(ctx, new_prefix: str):
    if len(new_prefix) > 5:
        return await ctx.send("Prefix must be 5 characters or fewer.")
    await set_setting(ctx.guild.id, "prefix", new_prefix)
    await ctx.send(f"Prefix changed to `{new_prefix}`")

@bot.command(name="serverinfo")
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=g.name, color=discord.Color.from_str("#010a17"))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="Owner", value=g.owner.mention)
    embed.add_field(name="Members", value=g.member_count)
    embed.add_field(name="Channels", value=len(g.channels))
    embed.add_field(name="Roles", value=len(g.roles))
    embed.add_field(name="Boosts", value=g.premium_subscription_count)
    embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    await ctx.send(embed=embed)

@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=str(member), color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id)
    embed.add_field(name="Nickname", value=member.nick or "None")
    embed.add_field(name="Top Role", value=member.top_role.mention)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
    embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)) as cur:
            count = (await cur.fetchone())[0]
    embed.add_field(name="Warnings", value=count)
    await ctx.send(embed=embed)

@bot.command(name="avatar")
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member}'s Avatar", color=discord.Color.from_str("#010a17"))
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="roleinfo")
async def roleinfo(ctx, role: discord.Role):
    embed = discord.Embed(title=f"Role: {role.name}", color=role.color)
    embed.add_field(name="ID", value=role.id)
    embed.add_field(name="Members", value=len(role.members))
    embed.add_field(name="Mentionable", value=role.mentionable)
    embed.add_field(name="Hoisted", value=role.hoist)
    embed.add_field(name="Position", value=role.position)
    embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
    await ctx.send(embed=embed)

@bot.command(name="invite")
async def invite(ctx):
    embed = discord.Embed(title="Invite Credentials",
                          description=f"[Click here](https://discord.com/oauth2/authorize?client_id={bot.user.id}&permissions=8&scope=bot)",
                          color=discord.Color.from_str("#010a17"))
    await ctx.send(embed=embed)

@bot.command(name="uptime")
async def uptime(ctx):
    delta = datetime.datetime.utcnow() - bot_start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    await ctx.send(f"Uptime: **{h}h {m}m {s}s**")

@bot.command(name="botinfo")
async def botinfo(ctx):
    embed = discord.Embed(title="Credentials Bot", color=discord.Color.from_str("#010a17"))
    embed.add_field(name="Servers", value=len(bot.guilds))
    embed.add_field(name="Users", value=sum(g.member_count for g in bot.guilds))
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms")
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)

# ─────────────────────────────────────────────
# MODERATION
# ─────────────────────────────────────────────

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if not await can_use_command(ctx.author, "ban"):
        return await ctx.send("You do not have the required role for this command.")
    if member.top_role >= ctx.author.top_role and not ctx.author.guild_permissions.administrator:
        return await ctx.send("You cannot ban someone with an equal or higher role.")
    await member.ban(reason=reason)
    await ctx.send(f"Banned **{member}**. Reason: {reason}")
    await log_action(ctx.guild, mod_embed("Member Banned", discord.Color.red(), user=str(member), moderator=str(ctx.author), reason=reason))

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int, *, reason: str = "No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=reason)
        await ctx.send(f"Unbanned **{user}**.")
        await log_action(ctx.guild, mod_embed("Member Unbanned", discord.Color.green(), user=str(user), moderator=str(ctx.author), reason=reason))
    except discord.NotFound:
        await ctx.send("User not found or not banned.")

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if not await can_use_command(ctx.author, "kick"):
        return await ctx.send("You do not have the required role for this command.")
    if member.top_role >= ctx.author.top_role and not ctx.author.guild_permissions.administrator:
        return await ctx.send("You cannot kick someone with an equal or higher role.")
    await member.kick(reason=reason)
    await ctx.send(f"Kicked **{member}**. Reason: {reason}")
    await log_action(ctx.guild, mod_embed("Member Kicked", discord.Color.orange(), user=str(member), moderator=str(ctx.author), reason=reason))

@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def timeout_cmd(ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    if not await can_use_command(ctx.author, "timeout"):
        return await ctx.send("You do not have the required role for this command.")
    seconds = parse_time(duration)
    if not seconds:
        return await ctx.send("Invalid time format. Use: 10s, 5m, 2h, 1d.")
    until = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    await ctx.send(f"Timed out **{member}** for `{duration}`. Reason: {reason}")
    await log_action(ctx.guild, mod_embed("Member Timed Out", discord.Color.orange(), user=str(member), duration=duration, moderator=str(ctx.author), reason=reason))

@bot.command(name="untimeout")
@commands.has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"Removed timeout from **{member}**.")

@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if not await can_use_command(ctx.author, "warn"):
        return await ctx.send("You do not have the required role for this command.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO warnings (guild_id, user_id, mod_id, reason, timestamp) VALUES (?,?,?,?,?)",
                         (ctx.guild.id, member.id, ctx.author.id, reason, datetime.datetime.utcnow().isoformat()))
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)) as cur:
            count = (await cur.fetchone())[0]
    await ctx.send(f"Warned **{member}** (Warning #{count}). Reason: {reason}")
    try:
        await member.send(f"You were warned in **{ctx.guild.name}**: {reason} (Warning #{count})")
    except Exception:
        pass
    await log_action(ctx.guild, mod_embed("Member Warned", discord.Color.yellow(), user=str(member), warning_num=count, moderator=str(ctx.author), reason=reason))

@bot.command(name="warnings")
async def warnings_cmd(ctx, member: discord.Member):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT reason, timestamp, mod_id FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id", (ctx.guild.id, member.id)) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await ctx.send(f"**{member}** has no warnings.")
    embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.yellow())
    for i, (reason, ts, mod_id) in enumerate(rows, 1):
        embed.add_field(name=f"#{i}", value=f"Reason: {reason}\nBy: <@{mod_id}>", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="clearwarnings")
@commands.has_permissions(manage_messages=True)
async def clearwarnings(ctx, member: discord.Member):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
        await db.commit()
    await ctx.send(f"Cleared all warnings for **{member}**.")

@bot.command(name="mute")
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, duration: str = None, *, reason: str = "No reason provided"):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not mute_role:
        mute_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            try:
                await channel.set_permissions(mute_role, send_messages=False, speak=False)
            except Exception:
                pass
    await member.add_roles(mute_role, reason=reason)
    if duration:
        seconds = parse_time(duration)
        if seconds:
            expires = (datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT OR REPLACE INTO mutes (guild_id, user_id, expires_at) VALUES (?,?,?)",
                                 (ctx.guild.id, member.id, expires))
                await db.commit()
    await ctx.send(f"Muted **{member}**{f' for {duration}' if duration else ''}. Reason: {reason}")
    await log_action(ctx.guild, mod_embed("Member Muted", discord.Color.dark_grey(), user=str(member), duration=duration or "Indefinite", moderator=str(ctx.author), reason=reason))

@bot.command(name="unmute")
@commands.has_permissions(manage_roles=True)
async def unmute(ctx, member: discord.Member):
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if mute_role and mute_role in member.roles:
        await member.remove_roles(mute_role)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM mutes WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
            await db.commit()
        await ctx.send(f"Unmuted **{member}**.")
    else:
        await ctx.send(f"**{member}** is not muted.")

@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int, member: discord.Member = None):
    if amount < 1 or amount > 500:
        return await ctx.send("Amount must be between 1 and 500.")
    check = (lambda m: m.author == member) if member else None
    deleted = await ctx.channel.purge(limit=amount, check=check)
    msg = await ctx.send(f"Deleted {len(deleted)} messages.")
    await asyncio.sleep(3)
    await msg.delete()

@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
async def lock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send(f"Locked {channel.mention}.")

@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    await channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send(f"Unlocked {channel.mention}.")

@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int):
    if seconds < 0 or seconds > 21600:
        return await ctx.send("Slowmode must be between 0 and 21600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"Set slowmode to {seconds}s.")

@bot.command(name="nick")
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    await member.edit(nick=nickname)
    await ctx.send(f"{'Reset' if not nickname else 'Changed'} nickname for **{member}**.")

# ─────────────────────────────────────────────
# ROLE MANAGEMENT
# ─────────────────────────────────────────────

@bot.group(name="role", invoke_without_command=True)
async def role(ctx):
    await ctx.send("Use `role add` or `role remove`.")

@role.command(name="add")
@commands.has_permissions(manage_roles=True)
async def role_add(ctx, member: discord.Member, role: discord.Role):
    await member.add_roles(role)
    await ctx.send(f"Added **{role.name}** to **{member}**.")

@role.command(name="remove")
@commands.has_permissions(manage_roles=True)
async def role_remove(ctx, member: discord.Member, role: discord.Role):
    await member.remove_roles(role)
    await ctx.send(f"Removed **{role.name}** from **{member}**.")

@bot.command(name="autorole")
@commands.has_permissions(manage_guild=True)
async def autorole(ctx, *, role_str: str):
    if role_str.lower() == "off":
        await set_setting(ctx.guild.id, "autorole", None)
        return await ctx.send("Autorole disabled.")
    try:
        role_id = int(role_str.strip("<@&>"))
        r = ctx.guild.get_role(role_id)
    except Exception:
        r = discord.utils.get(ctx.guild.roles, name=role_str)
    if not r:
        return await ctx.send("Role not found.")
    await set_setting(ctx.guild.id, "autorole", r.id)
    await ctx.send(f"Autorole set to **{r.name}**.")

@bot.group(name="bypass", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def bypass(ctx):
    await ctx.send("Use `bypass add`, `bypass remove`, or `bypass list`.")

@bypass.command(name="add")
@commands.has_permissions(administrator=True)
async def bypass_add(ctx, role: discord.Role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO bypass_roles (guild_id, role_id) VALUES (?,?)", (ctx.guild.id, role.id))
        await db.commit()
    await ctx.send(f"**{role.name}** bypasses all command restrictions.")

@bypass.command(name="remove")
@commands.has_permissions(administrator=True)
async def bypass_remove(ctx, role: discord.Role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bypass_roles WHERE guild_id=? AND role_id=?", (ctx.guild.id, role.id))
        await db.commit()
    await ctx.send(f"Removed bypass from **{role.name}**.")

@bypass.command(name="list")
@commands.has_permissions(administrator=True)
async def bypass_list(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT role_id FROM bypass_roles WHERE guild_id=?", (ctx.guild.id,)) as cur:
            rows = await cur.fetchall()
    if not rows:
        return await ctx.send("No bypass roles configured.")
    roles = [ctx.guild.get_role(r[0]) for r in rows]
    embed = discord.Embed(title="Bypass Roles", description="\n".join(r.mention for r in roles if r), color=discord.Color.from_str("#010a17"))
    await ctx.send(embed=embed)

@bot.group(name="cmdrole", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def cmdrole(ctx):
    await ctx.send("Use `cmdrole set <command> <role>` or `cmdrole clear <command>`.")

@cmdrole.command(name="set")
@commands.has_permissions(administrator=True)
async def cmdrole_set(ctx, command_name: str, role: discord.Role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO command_roles (guild_id, command_name, role_id) VALUES (?,?,?)", (ctx.guild.id, command_name, role.id))
        await db.commit()
    await ctx.send(f"**{role.name}** is required to use `{command_name}`.")

@cmdrole.command(name="clear")
@commands.has_permissions(administrator=True)
async def cmdrole_clear(ctx, command_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM command_roles WHERE guild_id=? AND command_name=?", (ctx.guild.id, command_name))
        await db.commit()
    await ctx.send(f"Cleared role restrictions for `{command_name}`.")

# ─────────────────────────────────────────────
# WELCOME / LEAVE / LOGGING
# ─────────────────────────────────────────────

@bot.group(name="welcome", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def welcome(ctx):
    await ctx.send("Use `welcome channel` or `welcome message`.")

@welcome.command(name="channel")
@commands.has_permissions(manage_guild=True)
async def welcome_channel(ctx, channel: discord.TextChannel):
    await set_setting(ctx.guild.id, "welcome_channel", channel.id)
    await ctx.send(f"Welcome channel set to {channel.mention}.")

@welcome.command(name="message")
@commands.has_permissions(manage_guild=True)
async def welcome_message(ctx, *, message: str):
    await set_setting(ctx.guild.id, "welcome_message", message)
    await ctx.send("Welcome message set. Variables: `{user}`, `{server}`, `{count}`")

@bot.group(name="leave", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def leave_group(ctx):
    await ctx.send("Use `leave channel` or `leave message`.")

@leave_group.command(name="channel")
@commands.has_permissions(manage_guild=True)
async def leave_channel(ctx, channel: discord.TextChannel):
    await set_setting(ctx.guild.id, "leave_channel", channel.id)
    await ctx.send(f"Leave channel set to {channel.mention}.")

@leave_group.command(name="message")
@commands.has_permissions(manage_guild=True)
async def leave_message(ctx, *, message: str):
    await set_setting(ctx.guild.id, "leave_message", message)
    await ctx.send("Leave message set. Variables: `{user}`, `{server}`")

@bot.command(name="logging")
@commands.has_permissions(manage_guild=True)
async def logging_cmd(ctx, channel_or_off: str):
    if channel_or_off.lower() == "off":
        await set_setting(ctx.guild.id, "log_channel", None)
        return await ctx.send("Logging disabled.")
    try:
        channel = ctx.guild.get_channel(int(channel_or_off.strip("<#>")))
    except Exception:
        channel = None
    if not channel:
        return await ctx.send("Channel not found.")
    await set_setting(ctx.guild.id, "log_channel", channel.id)
    await ctx.send(f"Logging set to {channel.mention}.")

# ─────────────────────────────────────────────
# GIVEAWAY
# ─────────────────────────────────────────────

@bot.group(name="giveaway", invoke_without_command=True)
async def giveaway(ctx):
    await ctx.send("Use `giveaway start`, `giveaway end`, or `giveaway reroll`.")

@giveaway.command(name="start")
@commands.has_permissions(manage_guild=True)
async def giveaway_start(ctx, duration: str, winners: int, *, prize: str):
    rigged_user = None
    if "--rig" in prize:
        parts = prize.split("--rig")
        prize = parts[0].strip()
        try:
            rigged_user = await commands.MemberConverter().convert(ctx, parts[1].strip())
        except Exception:
            pass
    seconds = parse_time(duration)
    if not seconds:
        return await ctx.send("Invalid duration. Use 10s, 5m, 2h, 1d.")
    ends_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    embed = discord.Embed(
        title=f"GIVEAWAY: {prize}",
        description=f"React with \U0001f389 to enter!\n\nWinners: **{winners}**\nEnds: {discord.utils.format_dt(ends_at, 'R')}",
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Hosted by {ctx.author}")
    embed.timestamp = ends_at
    msg = await ctx.send(embed=embed)
    await msg.add_reaction("\U0001f389")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, host_id, prize, winners, ends_at, rigged_user_id) VALUES (?,?,?,?,?,?,?,?)",
            (ctx.guild.id, ctx.channel.id, msg.id, ctx.author.id, prize, winners, ends_at.isoformat(), rigged_user.id if rigged_user else None)
        )
        await db.commit()
    notice = f"Giveaway started{f' (rigged)' if rigged_user else ''}!"
    await ctx.send(notice, delete_after=5)

@giveaway.command(name="end")
@commands.has_permissions(manage_guild=True)
async def giveaway_end(ctx, message_id: int):
    await end_giveaway(message_id, ctx.guild)

@giveaway.command(name="reroll")
@commands.has_permissions(manage_guild=True)
async def giveaway_reroll(ctx, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id, winners, rigged_user_id FROM giveaways WHERE message_id=? AND guild_id=?", (message_id, ctx.guild.id)) as cur:
            row = await cur.fetchone()
    if not row:
        return await ctx.send("Giveaway not found.")
    channel = ctx.guild.get_channel(row[0])
    try:
        msg = await channel.fetch_message(message_id)
        reaction = discord.utils.get(msg.reactions, emoji="\U0001f389")
        users = [u async for u in reaction.users() if not u.bot]
        if not users:
            return await ctx.send("No valid entries.")
        rigged_id = row[2]
        if rigged_id and any(u.id == rigged_id for u in users):
            winners = [u for u in users if u.id == rigged_id]
            extras = random.sample([u for u in users if u.id != rigged_id], min(row[1] - 1, len(users) - 1))
            winners = winners + extras
        else:
            winners = random.sample(users, min(row[1], len(users)))
        await ctx.send(f"Rerolled! New winner(s): {', '.join(w.mention for w in winners)}")
    except Exception as e:
        await ctx.send(f"Failed to reroll: {e}")

async def end_giveaway(message_id, guild):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_id, winners, prize, rigged_user_id FROM giveaways WHERE message_id=? AND guild_id=? AND ended=0", (message_id, guild.id)) as cur:
            row = await cur.fetchone()
        if not row:
            return
        await db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
        await db.commit()
    channel = guild.get_channel(row[0])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(message_id)
        reaction = discord.utils.get(msg.reactions, emoji="\U0001f389")
        if not reaction:
            return await channel.send("No entries for this giveaway.")
        users = [u async for u in reaction.users() if not u.bot]
        if not users:
            return await channel.send("No valid entries for this giveaway.")
        rigged_id = row[3]
        rigged_user = guild.get_member(rigged_id) if rigged_id else None
        if rigged_user and rigged_user in users:
            extra = random.sample([u for u in users if u != rigged_user], min(row[1] - 1, len(users) - 1))
            winners = [rigged_user] + extra
        else:
            winners = random.sample(users, min(row[1], len(users)))
        embed = discord.Embed(title=f"GIVEAWAY ENDED: {row[2]}", description=f"Winner(s): {', '.join(w.mention for w in winners)}", color=discord.Color.green())
        await msg.edit(embed=embed)
        await channel.send(f"Congratulations {', '.join(w.mention for w in winners)}! You won **{row[2]}**!")
    except Exception:
        pass

@tasks.loop(seconds=30)
async def check_giveaways():
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT message_id, guild_id FROM giveaways WHERE ended=0 AND ends_at <= ?", (now,)) as cur:
            rows = await cur.fetchall()
    for message_id, guild_id in rows:
        guild = bot.get_guild(guild_id)
        if guild:
            await end_giveaway(message_id, guild)

@tasks.loop(seconds=60)
async def check_mutes():
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, user_id FROM mutes WHERE expires_at <= ?", (now,)) as cur:
            rows = await cur.fetchall()
        for guild_id, user_id in rows:
            guild = bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(user_id)
                mute_role = discord.utils.get(guild.roles, name="Muted")
                if member and mute_role and mute_role in member.roles:
                    try:
                        await member.remove_roles(mute_role)
                    except Exception:
                        pass
            await db.execute("DELETE FROM mutes WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        await db.commit()

# ─────────────────────────────────────────────
# TICKETS
# ─────────────────────────────────────────────

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="credentials_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT category_id, support_role_id FROM ticket_settings WHERE guild_id=?", (interaction.guild.id,)) as cur:
                row = await cur.fetchone()
        if not row:
            return await interaction.response.send_message("Ticket system not configured.", ephemeral=True)
        category = interaction.guild.get_channel(row[0])
        support_role = interaction.guild.get_role(row[1])
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        channel = await interaction.guild.create_text_channel(f"ticket-{interaction.user.name}", category=category, overwrites=overwrites)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO tickets (guild_id, channel_id, user_id, created_at) VALUES (?,?,?,?)",
                             (interaction.guild.id, channel.id, interaction.user.id, datetime.datetime.utcnow().isoformat()))
            await db.commit()
        embed = discord.Embed(title="Ticket Opened", description=f"Hello {interaction.user.mention}, support will be with you shortly.", color=discord.Color.from_str("#010a17"))
        await channel.send(embed=embed)
        await interaction.response.send_message(f"Ticket opened: {channel.mention}", ephemeral=True)

@bot.group(name="ticket", invoke_without_command=True)
async def ticket(ctx):
    await ctx.send("Use `ticket setup`, `ticket panel`, `ticket close`, `ticket add`, or `ticket remove`.")

@ticket.command(name="setup")
@commands.has_permissions(manage_guild=True)
async def ticket_setup(ctx, category: discord.CategoryChannel, support_role: discord.Role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO ticket_settings (guild_id, category_id, support_role_id) VALUES (?,?,?)",
                         (ctx.guild.id, category.id, support_role.id))
        await db.commit()
    await ctx.send(f"Tickets configured. Category: **{category.name}**, Support Role: **{support_role.name}**")

@ticket.command(name="panel")
@commands.has_permissions(manage_guild=True)
async def ticket_panel(ctx, channel: discord.TextChannel = None):
    channel = channel or ctx.channel
    embed = discord.Embed(title="Support Tickets", description="Click the button below to open a support ticket.", color=discord.Color.from_str("#010a17"))
    await channel.send(embed=embed, view=TicketView())
    await ctx.send(f"Ticket panel sent to {channel.mention}.")

@ticket.command(name="close")
async def ticket_close(ctx):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id FROM tickets WHERE channel_id=? AND open=1", (ctx.channel.id,)) as cur:
            row = await cur.fetchone()
    if not row:
        return await ctx.send("This is not an active ticket channel.")
    await ctx.send("Closing ticket in 5 seconds...")
    await asyncio.sleep(5)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tickets SET open=0 WHERE id=?", (row[0],))
        await db.commit()
    await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")

@ticket.command(name="add")
async def ticket_add(ctx, member: discord.Member):
    await ctx.channel.set_permissions(member, read_messages=True, send_messages=True)
    await ctx.send(f"Added {member.mention} to the ticket.")

@ticket.command(name="remove")
async def ticket_remove(ctx, member: discord.Member):
    await ctx.channel.set_permissions(member, overwrite=None)
    await ctx.send(f"Removed {member.mention} from the ticket.")

# ─────────────────────────────────────────────
# FUN
# ─────────────────────────────────────────────

EIGHT_BALL = ["It is certain.", "Without a doubt.", "Yes, definitely.", "Most likely.", "Outlook good.",
              "Yes.", "Signs point to yes.", "Reply hazy, try again.", "Ask again later.",
              "Don't count on it.", "My reply is no.", "Outlook not so good.", "Very doubtful."]

JOKES = ["Why do programmers prefer dark mode? Because light attracts bugs.",
         "A SQL query walks into a bar and asks two tables: 'Can I join you?'",
         "Why do Python developers wear glasses? Because they can't C.",
         "Why did the developer quit? Because they didn't get arrays."]

FACTS = ["Honey never spoils — archaeologists found 3000-year-old honey in Egyptian tombs.",
         "Cleopatra lived closer to the Moon landing than to the construction of the Great Pyramid.",
         "The shortest war in history lasted 38 minutes, between Britain and Zanzibar in 1896.",
         "Octopuses have three hearts and blue blood.",
         "Bananas are berries. Strawberries are not."]

ROASTS = ["You're like a cloud. When you disappear, it's a beautiful day.",
          "I'd agree with you but then we'd both be wrong.",
          "You're not stupid, you just have bad luck thinking."]

COMPLIMENTS = ["You handle things with a rare kind of clarity.",
               "The way you think about problems is genuinely impressive.",
               "You make everyone around you feel more at ease."]

@bot.command(name="8ball")
async def eightball(ctx, *, question: str):
    await ctx.send(f"**{question}**\n{random.choice(EIGHT_BALL)}")

@bot.command(name="coinflip")
async def coinflip(ctx):
    await ctx.send(f"The coin landed on **{random.choice(['Heads', 'Tails'])}**.")

@bot.command(name="roll")
async def roll(ctx, sides: int = 6):
    if sides < 2:
        return await ctx.send("A die needs at least 2 sides.")
    await ctx.send(f"You rolled a **{random.randint(1, sides)}** (d{sides}).")

@bot.command(name="rps")
async def rps(ctx, choice: str):
    choices = ["rock", "paper", "scissors"]
    choice = choice.lower()
    if choice not in choices:
        return await ctx.send("Choose rock, paper, or scissors.")
    bot_choice = random.choice(choices)
    outcomes = {("rock", "scissors"): "You win!", ("scissors", "paper"): "You win!", ("paper", "rock"): "You win!",
                ("rock", "paper"): "Bot wins!", ("scissors", "rock"): "Bot wins!", ("paper", "scissors"): "Bot wins!"}
    result = outcomes.get((choice, bot_choice), "It's a tie!")
    await ctx.send(f"You: **{choice}** | Bot: **{bot_choice}** | {result}")

@bot.command(name="choose")
async def choose(ctx, *, options: str):
    choices = [o.strip() for o in options.split("|")]
    if len(choices) < 2:
        return await ctx.send("Provide at least two options separated by `|`.")
    await ctx.send(f"I choose: **{random.choice(choices)}**")

@bot.command(name="joke")
async def joke(ctx):
    await ctx.send(random.choice(JOKES))

@bot.command(name="fact")
async def fact(ctx):
    await ctx.send(random.choice(FACTS))

@bot.command(name="roast")
async def roast(ctx, member: discord.Member = None):
    target = member or ctx.author
    await ctx.send(f"{target.mention}, {random.choice(ROASTS)}")

@bot.command(name="compliment")
async def compliment(ctx, member: discord.Member = None):
    target = member or ctx.author
    await ctx.send(f"{target.mention}, {random.choice(COMPLIMENTS)}")

@bot.command(name="ship")
async def ship(ctx, user1: discord.Member, user2: discord.Member):
    score = (user1.id + user2.id) % 101
    bar = "=" * (score // 10) + "-" * (10 - score // 10)
    await ctx.send(f"{user1.display_name} + {user2.display_name}\n[{bar}] **{score}%** compatible")

@bot.command(name="rate")
async def rate(ctx, *, thing: str):
    random.seed(thing + str(ctx.guild.id))
    await ctx.send(f"I rate **{thing}** a **{random.randint(0, 10)}/10**.")

@bot.command(name="mock")
async def mock(ctx, *, text: str):
    await ctx.send("".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(text)))

# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False, debug=False),
        daemon=True
    )
    flask_thread.start()
    print(f"Dashboard running on port {port}")
    bot.run(DISCORD_TOKEN)
