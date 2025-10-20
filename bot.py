import os
import sqlite3
import time
import json
from typing import Optional, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()

# ---------------- CONFIG ----------------

PREFIX = "!"
DEFAULT_COOLDOWN_SECONDS = 20   # Prevents spam; used if a guild hasn't set one yet
cooldown_cache: dict[int, int] = {}  # guild_id -> seconds (filled on_ready)
MIN_COUNTABLE_LEN = 3         # ignore super short messages

# Level thresholds (highest first)
LEVELS = [
    ("LVMAX", 1000),
    ("LV3", 100),
    ("LV2", 10),
    ("LV1", 0),
]

# Announce channel (optional): put your channel ID here or leave None
ANNOUNCE_CHANNEL_ID = None  # e.g., 123456789012345678

# Database (point to a persistent mount in hosting)
DB_PATH = os.getenv("DB_PATH", "levels.db")

# Directory to store manual counts as text files (JSON content)
# Will create per-guild subfolder inside this.
USER_COUNTS_DIR = os.getenv("USER_COUNTS_DIR", "User Message Counts")

# ---------------- DATABASE (live counts) ----------------

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Table for message counts
    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_counts (
            guild_id INTEGER NOT NULL,
            user_id  INTEGER NOT NULL,
            count    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    # Table for per-guild settings (cooldown, etc.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            cooldown_seconds INTEGER
        )
    """)
    con.commit()
    con.close()

def migrate_guild_settings():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Create table if it doesn't exist (older versions may have had fewer columns)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY
            -- columns may be added below by migration
        )
    """)

    # Inspect existing columns
    cur.execute("PRAGMA table_info(guild_settings)")
    cols = {row[1] for row in cur.fetchall()}

    # Add missing column(s) safely
    if "announce_channel_id" not in cols:
        cur.execute("ALTER TABLE guild_settings ADD COLUMN announce_channel_id INTEGER")

    con.commit()
    con.close()

def get_live_count(guild_id: int, user_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT count FROM message_counts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    con.close()
    return row[0] if row else 0

def add_live_count(guild_id: int, user_id: int, delta: int = 1) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO message_counts (guild_id, user_id, count)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + excluded.count
    """, (guild_id, user_id, delta))
    con.commit()
    cur.execute("SELECT count FROM message_counts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    new_count = cur.fetchone()[0]
    con.close()
    return new_count

def get_cooldown_db(guild_id: int) -> int | None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT cooldown_seconds FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] is not None else None

def set_cooldown_db(guild_id: int, seconds: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO guild_settings (guild_id, cooldown_seconds)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET cooldown_seconds=excluded.cooldown_seconds
    """, (guild_id, seconds))
    con.commit()
    con.close()

def get_cooldown(guild_id: int) -> int:
    # Fast path: memory
    if guild_id in cooldown_cache:
        return cooldown_cache[guild_id]
    # DB or default
    val = get_cooldown_db(guild_id)
    seconds = val if val is not None else DEFAULT_COOLDOWN_SECONDS
    cooldown_cache[guild_id] = seconds
    return seconds

def set_cooldown(guild_id: int, seconds: int) -> None:
    set_cooldown_db(guild_id, seconds)
    cooldown_cache[guild_id] = seconds

# ---------------- MANUAL COUNTS (text files) ----------------

def _guild_dir(guild_id: int) -> str:
    # Per-guild subfolder to prevent cross-server collisions
    return os.path.join(USER_COUNTS_DIR, f"guild_{guild_id}")

def _user_file_path(guild_id: int, user_id: int) -> str:
    # One text file per user; filename is the user ID (as requested)
    # Using .txt but storing JSON inside (human-readable, extensible)
    return os.path.join(_guild_dir(guild_id), f"{user_id}.txt")

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def get_adjusted_count(guild_id: int, user_id: int) -> int:
    """
    Reads adjusted_message_count from the user's text file.
    Returns 0 if file doesn't exist or is invalid.
    """
    try:
        path = _user_file_path(guild_id, user_id)
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        val = int(data.get("adjusted_message_count", 0))
        return max(0, val)
    except Exception:
        return 0

def set_adjusted_count(guild_id: int, user_id: int, adjusted_value: int) -> None:
    """
    Writes adjusted_message_count to the user's text file.
    """
    ensure_dir(_guild_dir(guild_id))
    path = _user_file_path(guild_id, user_id)
    data = {"adjusted_message_count": max(0, int(adjusted_value))}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_total_count(guild_id: int, user_id: int) -> Tuple[int, int, int]:
    """
    Returns tuple: (total, adjusted, live)
    total = adjusted + live
    """
    live = get_live_count(guild_id, user_id)
    adjusted = get_adjusted_count(guild_id, user_id)
    return adjusted + live, adjusted, live

# ---------------- BOT SETUP ----------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)
last_increment = {}  # cooldown tracker

# ---------------- HELPERS ----------------

def get_target_level(count: int) -> Tuple[str, int]:
    for name, threshold in LEVELS:
        if count >= threshold:
            return name, threshold
    return "LV1", 0

def get_next_threshold(count: int):
    for name, threshold in reversed(LEVELS):
        if count < threshold:
            return name, threshold
    return None, None

def find_role_by_name(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    for role in guild.roles:
        if role.name.lower() == name.lower():
            return role
    return None

def _top_message_counts(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    """
    Return [(user_id, total_messages)] sorted desc by total.
    Combines live counts from SQLite with adjusted counts from the per-user JSON files.
    """
    # 1) get all live counts from DB
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT user_id, count FROM message_counts WHERE guild_id=?", (guild_id,))
    rows = cur.fetchall()
    con.close()

    totals: dict[int, int] = {int(uid): int(cnt) for (uid, cnt) in rows}

    # 2) add adjusted counts for anyone with a file
    gdir = _guild_dir(guild_id)
    if os.path.isdir(gdir):
        for fname in os.listdir(gdir):
            if not fname.endswith(".txt"):  # we store JSON inside .txt
                continue
            try:
                uid = int(os.path.splitext(fname)[0])
            except ValueError:
                continue
            adj = get_adjusted_count(guild_id, uid)
            if uid in totals:
                totals[uid] += adj
            else:
                totals[uid] = adj

    # 3) sort and cap
    items = sorted(totals.items(), key=lambda x: (-x[1], x[0]))
    return items[:max(1, min(25, limit))]

async def ensure_lv_role(member: discord.Member, level_name: str):
    guild = member.guild
    role_targets = {name: find_role_by_name(guild, name) for name, _ in LEVELS}
    target_role = role_targets.get(level_name)

    if target_role is None:
        return False, f"Missing `{level_name}` role. Ask an admin to create it."

    roles_to_remove = [r for name, r in role_targets.items()
                       if r and name != level_name and r in member.roles]

    changed = False
    try:
        if target_role not in member.roles:
            await member.add_roles(target_role, reason=f"Reached {level_name}")
            changed = True

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=f"Switched to {level_name}")
            changed = True

        return changed, f"You now have **{level_name}**."
    except discord.Forbidden:
        return False, "I need **Manage Roles** and my role must be above LV roles."
    except discord.HTTPException:
        return False, "Discord API error while assigning roles."

async def announce_level_up(guild: discord.Guild, target: discord.Member, level_name: str, total: int, fallback_channel):
    channel = fallback_channel
    stored_id = get_announce_channel(guild.id)
    if stored_id:
        ch = guild.get_channel(stored_id)
        if isinstance(ch, discord.TextChannel):
            channel = ch

    next_name, next_thr = get_next_threshold(total)
    next_line = f"Next: **{next_name}** at `{next_thr}` messages." if next_name else "You’ve reached **LVMAX**! 🔥"

    embed = discord.Embed(
        title="🎉 Level Up!",
        description=f"{target.mention} just reached **{level_name}**!\n\n**Messages:** `{total}`\n{next_line}"
    )
    embed.set_footer(text="Keep chatting and being awesome.")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        await channel.send(f"🎉 {target.mention} reached **{level_name}**! ({total} messages)")

def get_announce_channel(guild_id: int) -> Optional[int]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT announce_channel_id FROM guild_settings WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    con.close()
    return row[0] if row and row[0] else None

def set_announce_channel(guild_id: int, channel_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO guild_settings (guild_id, announce_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET announce_channel_id=excluded.announce_channel_id
    """, (guild_id, channel_id))
    con.commit()
    con.close()

# ---------------- EVENTS ----------------

@bot.event
async def on_ready():
    init_db()
    migrate_guild_settings()
    # Ensure base folder exists
    ensure_dir(USER_COUNTS_DIR)

    # 🔹 Warm cooldown cache for joined guilds
    for g in bot.guilds:
        seconds = get_cooldown_db(g.id)
        cooldown_cache[g.id] = seconds if seconds is not None else DEFAULT_COOLDOWN_SECONDS
        
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_member_join(member: discord.Member):
    role = find_role_by_name(member.guild, "LV1")
    if role:
        try:
            await member.add_roles(role, reason="Default LV1 on join")
        except discord.Forbidden:
            pass

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if not message.guild or message.author.bot:
        return
    if len(message.content.strip()) < MIN_COUNTABLE_LEN:
        return

    key = (message.guild.id, message.author.id)
    now = time.time()
    guild_cd = get_cooldown(message.guild.id)
    if now - last_increment.get(key, 0) < guild_cd:
        return

    new_live = add_live_count(message.guild.id, message.author.id, 1)
    last_increment[key] = now

    # Optional auto-leveling mid-chat
    total, adjusted, live = get_total_count(message.guild.id, message.author.id)
    new_level, _ = get_target_level(total)
    role = find_role_by_name(message.guild, new_level)
    if role and role not in message.author.roles:
        changed, _ = await ensure_lv_role(message.author, new_level)
        if changed:
            await announce_level_up(message.guild, message.author, new_level, total, message.channel)

@bot.event
async def setup_hook():
    # Load cogs/extensions at startup
    await bot.load_extension("cogs.coins")
    await bot.load_extension("cogs.shop")
    await bot.load_extension("cogs.game")

# ---------------- COMMANDS ----------------

@bot.command(name="lv")
async def lv(ctx: commands.Context, member: Optional[discord.Member] = None):
    """
    !lv            -> check your own count + auto-assign correct LV role
    !lv @someone   -> (mods only) check & correct someone else's LV role
    """
    target = member or ctx.author

    if target != ctx.author:
        perms = ctx.channel.permissions_for(ctx.author)
        if not (perms.manage_roles or perms.administrator):
            return await ctx.reply("You can only check your own level.", mention_author=False)

    total, adjusted, live = get_total_count(ctx.guild.id, target.id)
    level_name, threshold = get_target_level(total)
    changed, msg = await ensure_lv_role(target, level_name)
    own = "Your" if target == ctx.author else f"{target.display_name}'s"

    await ctx.reply(
        f"{own} **message count**: `{total}`  (adjusted: `{adjusted}`, live: `{live}`)\n"
        f"{own} **level**: **{level_name}** (≥ {threshold})\n"
        f"{msg}",
        mention_author=False
    )

    if changed:
        await announce_level_up(ctx.guild, target, level_name, total, ctx.channel)

@bot.command(name="lvup")
@commands.has_permissions(manage_roles=True)
async def lvup(ctx: commands.Context, channel: discord.TextChannel):
    """Set the channel where level-up messages appear."""
    set_announce_channel(ctx.guild.id, channel.id)
    await ctx.reply(f"✅ Level-up announcements will now appear in {channel.mention}.", mention_author=False)

@bot.command(name="lv_cooldown")
@commands.has_permissions(manage_roles=True)
async def lv_cooldown(ctx: commands.Context, seconds: Optional[int] = None):
    """
    View or change the cooldown between message counts (per guild).
    - !lv_cooldown         -> show current cooldown
    - !lv_cooldown 10      -> set to 10 seconds
    """
    if seconds is None:
        current = get_cooldown(ctx.guild.id)
        return await ctx.reply(f"Current cooldown is `{current}` seconds.", mention_author=False)

    if seconds < 0:
        return await ctx.reply("Cooldown must be non-negative.", mention_author=False)

    old = get_cooldown(ctx.guild.id)
    set_cooldown(ctx.guild.id, seconds)
    await ctx.reply(f"✅ Cooldown changed from `{old}`s to `{seconds}`s.", mention_author=False)

@bot.command(name="lv_get")
async def lv_get(ctx: commands.Context, member: Optional[discord.Member] = None):
    """
    Show breakdown without changing roles.
    """
    target = member or ctx.author
    total, adjusted, live = get_total_count(ctx.guild.id, target.id)
    level_name, threshold = get_target_level(total)
    await ctx.reply(
        f"{target.display_name}'s counts → total: `{total}`, adjusted: `{adjusted}`, live: `{live}`\n"
        f"Current level: **{level_name}** (≥ {threshold})",
        mention_author=False
    )

@bot.command(name="lv_set")
@commands.has_permissions(manage_roles=True)
async def lv_set(ctx: commands.Context, member: discord.Member, provided_total: int):
    """
    Set a user's total messages manually.
    We store it as adjusted_message_count so that:
      adjusted = max(0, provided_total - current_live)
      total now = adjusted + live = provided_total
    """
    if provided_total < 0:
        return await ctx.reply("Total must be non-negative.", mention_author=False)

    _, _, live = get_total_count(ctx.guild.id, member.id)
    adjusted = max(0, provided_total - live)
    set_adjusted_count(ctx.guild.id, member.id, adjusted)

    total, adjusted_now, live_now = get_total_count(ctx.guild.id, member.id)
    level_name, threshold = get_target_level(total)

    # Ensure role matches new total
    changed, msg = await ensure_lv_role(member, level_name)

    await ctx.reply(
        f"Set **{member.display_name}** total to `{provided_total}` → stored adjusted: `{adjusted}` (live: `{live}`)\n"
        f"Current totals → total: `{total}`, adjusted: `{adjusted_now}`, live: `{live_now}`\n"
        f"Level: **{level_name}** (≥ {threshold})\n{msg}",
        mention_author=False
    )

@bot.command(name="toplv", aliases=["topmsgs", "topmessages"])
async def toplv(ctx: commands.Context, limit: int = 10):
    """
    Show the top message counts (LV leaderboard). Default top 10 (max 25).
    Usage: !toplv        or  !toplv 15
    """
    gid = ctx.guild.id
    limit = max(1, min(25, int(limit)))

    rows = _top_message_counts(gid, limit)
    if not rows:
        return await ctx.reply("No message data yet. Start chatting and try again!", mention_author=False)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for idx, (uid, total) in enumerate(rows, start=1):
        member = ctx.guild.get_member(uid)
        name = member.display_name if member else f"User {uid}"
        lv_name, _ = get_target_level(total)
        tag = medals.get(idx, f"{idx}.")
        lines.append(f"{tag}  {name} — **{total}** msgs  |  {lv_name}")

    embed = discord.Embed(
        title=f"🏆 Top {len(rows)} Message Counts",
        description="\n".join(lines),
        color=discord.Color.blurple()
    )
    if ctx.guild.icon:
        embed.set_thumbnail(url=ctx.guild.icon.url)

    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="lv_syncall")
@commands.has_permissions(manage_roles=True)
async def lv_syncall(ctx: commands.Context):
    await ctx.reply("Syncing LV roles...", mention_author=False)
    synced = 0
    for member in ctx.guild.members:
        if member.bot:
            continue
        total, _, _ = get_total_count(ctx.guild.id, member.id)
        level_name, _ = get_target_level(total)
        changed, _ = await ensure_lv_role(member, level_name)
        if changed:
            synced += 1
    await ctx.send(f"✅ LV Sync complete. Updated {synced} members.")

# ---------------- RUN BOT ----------------

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("❌ Set DISCORD_TOKEN environment variable first.")
    # Make sure top-level folder exists (Railway volume-friendly)
    os.makedirs(USER_COUNTS_DIR, exist_ok=True)
    bot.run(token)
