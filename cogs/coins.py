# cogs/coins.py
import os
import time
import sqlite3
from typing import Tuple, Dict

import discord
from discord.ext import commands

DB_PATH = os.getenv("DB_PATH", "levels.db")  # reuse same DB file

# Defaults when a guild has no explicit settings yet
DEFAULT_REWARD = 10
DEFAULT_COOLDOWN = 24 * 60 * 60  # 24h
DEFAULT_SYMBOL = "🪙"             # shown LEFT of the number, e.g., 🪙300


class Coins(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        # cache: {guild_id: (reward, cooldown, symbol)}
        self.settings_cache: Dict[int, Tuple[int, int, str]] = {}

    # ---------- DB bootstrap / migration ----------
    def _con(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        con = self._con()
        cur = con.cursor()

        # Users' coin balances and last claim times
        cur.execute("""
            CREATE TABLE IF NOT EXISTS coins (
                guild_id   INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                balance    INTEGER NOT NULL DEFAULT 0,
                last_claim INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)

        # Guild settings base table (your main bot already creates it; we migrate if needed)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                cooldown_seconds INTEGER
            )
        """)

        # --- MIGRATION: ensure columns exist for claim + currency
        cur.execute("PRAGMA table_info(guild_settings)")
        existing = {row[1] for row in cur.fetchall()}
        if "claim_reward" not in existing:
            cur.execute("ALTER TABLE guild_settings ADD COLUMN claim_reward INTEGER")
        if "claim_cooldown" not in existing:
            cur.execute("ALTER TABLE guild_settings ADD COLUMN claim_cooldown INTEGER")
        if "currency_symbol" not in existing:
            cur.execute("ALTER TABLE guild_settings ADD COLUMN currency_symbol TEXT")

        con.commit()
        con.close()

    # ---------- settings helpers ----------
    def _get_settings_db(self, guild_id: int) -> Tuple[int, int, str]:
        """Return (reward, cooldown, symbol) from DB or defaults (no cache)."""
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT claim_reward, claim_cooldown, currency_symbol
            FROM guild_settings WHERE guild_id=?
        """, (guild_id,))
        row = cur.fetchone()
        con.close()
        reward  = row[0] if row and row[0] is not None else DEFAULT_REWARD
        cooldown = row[1] if row and row[1] is not None else DEFAULT_COOLDOWN
        symbol  = row[2] if row and row[2] else DEFAULT_SYMBOL
        return int(reward), int(cooldown), str(symbol)

    def _set_settings_db(self, guild_id: int, reward: int, cooldown: int, symbol: str) -> None:
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO guild_settings (guild_id, claim_reward, claim_cooldown, currency_symbol)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
              claim_reward=excluded.claim_reward,
              claim_cooldown=excluded.claim_cooldown,
              currency_symbol=excluded.currency_symbol
        """, (guild_id, int(reward), int(cooldown), symbol))
        con.commit()
        con.close()

    def get_settings(self, guild_id: int) -> Tuple[int, int, str]:
        if guild_id in self.settings_cache:
            return self.settings_cache[guild_id]
        reward, cooldown, symbol = self._get_settings_db(guild_id)
        self.settings_cache[guild_id] = (reward, cooldown, symbol)
        return reward, cooldown, symbol

    def set_settings(self, guild_id: int, reward: int, cooldown: int, symbol: str) -> None:
        self._set_settings_db(guild_id, reward, cooldown, symbol)
        self.settings_cache[guild_id] = (reward, cooldown, symbol)

    def fmt(self, guild_id: int, amount: int) -> str:
        """Format amount with symbol on the LEFT (e.g., 🪙300 or <:coin:123>300)."""
        _, _, symbol = self.get_settings(guild_id)
        return f"{symbol}{amount}"

    # ---------- user helpers ----------
    def _get_user(self, guild_id: int, user_id: int) -> Tuple[int, int]:
        """Return (balance, last_claim). Creates row if missing."""
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT balance, last_claim FROM coins WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO coins (guild_id, user_id, balance, last_claim) VALUES (?, ?, 0, 0)",
                        (guild_id, user_id))
            con.commit()
            row = (0, 0)
        con.close()
        return int(row[0]), int(row[1])

    def _set_user(self, guild_id: int, user_id: int, balance: int, last_claim: int) -> None:
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO coins (guild_id, user_id, balance, last_claim)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              balance=excluded.balance, last_claim=excluded.last_claim
        """, (guild_id, user_id, int(balance), int(last_claim)))
        con.commit()
        con.close()

    def _add_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        """Adjust balance by delta (can be negative). Returns new balance."""
        bal, last = self._get_user(guild_id, user_id)
        new_bal = max(0, bal + int(delta))  # never below zero
        self._set_user(guild_id, user_id, new_bal, last)
        return new_bal
    
    def _top_balances(self, guild_id: int, limit: int = 10):
        """Return list of rows: [(user_id, balance), ...] ordered by balance desc."""
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT user_id, balance
            FROM coins
            WHERE guild_id=?
            ORDER BY balance DESC, user_id ASC
            LIMIT ?
        """, (guild_id, limit))
        rows = cur.fetchall()
        con.close()
        return [(int(uid), int(bal)) for uid, bal in rows]

    # ---------- lifecycle ----------
    @commands.Cog.listener()
    async def on_ready(self):
        self._init_db()
        # warm cache
        for g in self.bot.guilds:
            self.settings_cache[g.id] = self._get_settings_db(g.id)

    # ---------- commands ----------
    @commands.command(name="claim")
    async def claim(self, ctx: commands.Context):
        """Claim daily coins. Cooldown & reward are per-guild settings."""
        gid, uid = ctx.guild.id, ctx.author.id
        reward, cooldown, _ = self.get_settings(gid)

        bal, last = self._get_user(gid, uid)
        now = int(time.time())
        remaining = cooldown - (now - last)
        if last != 0 and remaining > 0:
            hrs = remaining // 3600
            mins = (remaining % 3600) // 60
            secs = remaining % 60
            return await ctx.reply(
                f"You already claimed. Come back in **{hrs}h {mins}m {secs}s**.",
                mention_author=False
            )

        bal += reward
        self._set_user(gid, uid, bal, now)
        await ctx.reply(
            f"✅ Claimed **{self.fmt(gid, reward)}**! New balance: **{self.fmt(gid, bal)}**.",
            mention_author=False
        )

    @commands.command(name="coins", aliases=["balance", "bal"])
    async def coins(self, ctx: commands.Context, member: discord.Member | None = None):
        """Check your (or someone else’s) coin balance."""
        target = member or ctx.author
        bal, _ = self._get_user(ctx.guild.id, target.id)
        own = "Your" if target == ctx.author else f"{target.display_name}'s"
        await ctx.reply(f"{own} balance: **{self.fmt(ctx.guild.id, bal)}**.", mention_author=False)

    @commands.command(name="sharecoins", aliases=["pay", "tip", "sendcoins"])
    async def sharecoins(self, ctx: commands.Context, member: discord.Member, amount: int):
        """
        Give some of your own coins to another user.
        Usage: !sharecoins @user 50
        """
        gid = ctx.guild.id
        sender = ctx.author

        # Basic validation
        if amount <= 0:
            return await ctx.reply("Amount must be a positive integer.", mention_author=False)
        if member.bot:
            return await ctx.reply("Bots don’t need money!", mention_author=False)
        if member.id == sender.id:
            return await ctx.reply("You can’t send coins to yourself.", mention_author=False)

        # Ensure both users have rows and check balance
        sender_bal, sender_last = self._get_user(gid, sender.id)
        if sender_bal < amount:
            return await ctx.reply(
                f"You don’t have enough coins. Your balance is **{self.fmt(gid, sender_bal)}**.",
                mention_author=False
            )

        # Do the transfer (simple, safe updates)
        receiver_bal, receiver_last = self._get_user(gid, member.id)

        new_sender_bal = sender_bal - amount
        new_receiver_bal = receiver_bal + amount

        # Persist
        self._set_user(gid, sender.id, new_sender_bal, sender_last)
        self._set_user(gid, member.id, new_receiver_bal, receiver_last)

        await ctx.reply(
            f"✅ Transferred **{self.fmt(gid, amount)}** to **{member.display_name}**.\n"
            f"Your new balance: **{self.fmt(gid, new_sender_bal)}**. "
            f"{member.display_name}'s new balance: **{self.fmt(gid, new_receiver_bal)}**.",
            mention_author=False
        )

    @commands.command(name="topcoins", aliases=["topbal", "richest", "top"])
    async def topcoins(self, ctx: commands.Context, limit: int = 10):
        """
        Show the top coin holders (default top 10).
        Usage: !topcoins   /  !topcoins 25 (max 25)
        """
        gid = ctx.guild.id
        limit = max(1, min(25, int(limit)))  # clamp 1..25

        rows = self._top_balances(gid, limit)
        if not rows:
            return await ctx.reply("No coin data yet. Try `!claim` to get started!", mention_author=False)

        # build lines
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for idx, (uid, bal) in enumerate(rows, start=1):
            member = ctx.guild.get_member(uid)
            name = member.display_name if member else f"User {uid}"
            tag = medals.get(idx, f"{idx}.")
            lines.append(f"{tag}  {name} — **{self.fmt(gid, bal)}**")

        # embed
        embed = discord.Embed(
            title=f"🏆 Top {len(rows)} Coin Holders",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="givecoins")
    @commands.has_permissions(manage_roles=True)
    async def givecoins(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Give coins to a user (mods only)."""
        if amount <= 0:
            return await ctx.reply("Amount must be a positive integer.", mention_author=False)
        if member.bot:
            return await ctx.reply("Bots don’t need money. 😉", mention_author=False)

        gid = ctx.guild.id
        new_bal = self._add_balance(gid, member.id, amount)
        await ctx.reply(
            f"✅ Gave **{self.fmt(gid, amount)}** to **{member.display_name}**. "
            f"New balance: **{self.fmt(gid, new_bal)}**.",
            mention_author=False
        )

    @commands.command(name="takecoins")
    @commands.has_permissions(manage_roles=True)
    async def takecoins(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Remove coins from a user (mods only)."""
        if amount <= 0:
            return await ctx.reply("Amount must be a positive integer.", mention_author=False)
        if member.bot:
            return await ctx.reply("Bots don’t have balances to deduct.", mention_author=False)

        gid = ctx.guild.id
        current, _ = self._get_user(gid, member.id)
        deducted = min(amount, current)  # clamp so we don't go negative
        new_bal = self._add_balance(gid, member.id, -deducted)

        if deducted < amount:
            note = f" (requested {self.fmt(gid, amount)}, but user only had {self.fmt(gid, current)})"
        else:
            note = ""

        await ctx.reply(
            f"✅ Took **{self.fmt(gid, deducted)}** from **{member.display_name}**{note}. "
            f"New balance: **{self.fmt(gid, new_bal)}**.",
            mention_author=False
        )

    # ---------- admin: configure claim settings ----------
    @commands.command(name="claimcfg")
    @commands.has_permissions(manage_roles=True)
    async def claimcfg(self, ctx: commands.Context, sub: str | None = None, value: int | None = None):
        """
        Configure claim settings (per guild).
        - !claimcfg                     -> show current reward & cooldown
        - !claimcfg reward <amount>     -> set reward per claim
        - !claimcfg cooldown <seconds>  -> set claim cooldown
        """
        gid = ctx.guild.id
        reward, cooldown, symbol = self.get_settings(gid)

        if sub is None:
            return await ctx.reply(
                f"Current claim settings → reward: **{self.fmt(gid, reward)}**, cooldown: **{cooldown}**s.",
                mention_author=False
            )

        sub = sub.lower()
        if sub == "reward":
            if value is None or value < 0:
                return await ctx.reply("Provide a non-negative amount, e.g. `!claimcfg reward 10`.", mention_author=False)
            self.set_settings(gid, value, cooldown, symbol)
            return await ctx.reply(f"✅ Claim reward set to **{self.fmt(gid, value)}** (was {self.fmt(gid, reward)}).", mention_author=False)

        if sub == "cooldown":
            if value is None or value < 0:
                return await ctx.reply("Provide a non-negative number of seconds, e.g. `!claimcfg cooldown 86400`.", mention_author=False)
            self.set_settings(gid, reward, value, symbol)
            return await ctx.reply(f"✅ Claim cooldown set to **{value}**s (was {cooldown}s).", mention_author=False)

        return await ctx.reply("Unknown option. Use `!claimcfg`, `!claimcfg reward <n>`, or `!claimcfg cooldown <sec>`.",
                               mention_author=False)

    # ---------- admin: set currency symbol ----------
    @commands.command(name="currency")
    @commands.has_permissions(manage_roles=True)
    async def currency(self, ctx: commands.Context, *, symbol: str | None = None):
        """
        View or set the currency symbol (Unicode or custom emoji).
        Usage:
          - !currency                 -> show current symbol
          - !currency 🪙             -> set to a Unicode symbol
          - !currency <:coin:123>    -> set to a server emoji
        """
        gid = ctx.guild.id
        reward, cooldown, cur_symbol = self.get_settings(gid)

        if symbol is None:
            return await ctx.reply(
                f"Current currency symbol: **{cur_symbol}**  Example: {cur_symbol}300",
                mention_author=False
            )

        # Try to parse a custom emoji; fall back to raw text (Unicode)
        try:
            pe = await commands.PartialEmojiConverter().convert(ctx, symbol)
            new_symbol = pe.mention  # stores as '<:name:id>' or '<a:name:id>'
        except commands.BadArgument:
            new_symbol = symbol.strip()

        # Basic sanity: don't allow an empty symbol
        if not new_symbol:
            return await ctx.reply("Invalid symbol.", mention_author=False)

        self.set_settings(gid, reward, cooldown, new_symbol)
        await ctx.reply(
            f"✅ Currency symbol updated to **{new_symbol}**. Example: {new_symbol}300",
            mention_author=False
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Coins(bot))