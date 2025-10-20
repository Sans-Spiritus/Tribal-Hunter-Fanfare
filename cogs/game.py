# cogs/game.py
import os
import random
import sqlite3
from typing import Dict, Tuple, List
from cogs.utils.levels import _meets_level

import discord
from discord.ext import commands

DB_PATH = os.getenv("DB_PATH", "levels.db")
DEFAULT_SYMBOL = "🪙"

# --- Level reading helpers (match bot.py storage) ---
LEVELS = [("LVMAX", 1000), ("LV3", 100), ("LV2", 10), ("LV1", 0)]
USER_COUNTS_DIR = os.getenv("USER_COUNTS_DIR", "User Message Counts")

def _guild_dir(gid: int) -> str:
    return os.path.join(USER_COUNTS_DIR, f"guild_{gid}")

def _user_file_path(gid: int, uid: int) -> str:
    return os.path.join(_guild_dir(gid), f"{uid}.txt")

def _get_adjusted_count(gid: int, uid: int) -> int:
    try:
        path = _user_file_path(gid, uid)
        if not os.path.exists(path): return 0
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return max(0, int(data.get("adjusted_message_count", 0)))
    except Exception:
        return 0

def _get_live_count(db_path: str, gid: int, uid: int) -> int:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT count FROM message_counts WHERE guild_id=? AND user_id=?", (gid, uid))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0

def _get_total_and_level(db_path: str, gid: int, uid: int) -> tuple[int, str]:
    total = _get_live_count(db_path, gid, uid) + _get_adjusted_count(gid, uid)
    for name, thr in LEVELS:
        if total >= thr:
            return total, name
    return total, "LV1"

def _meets_level(db_path: str, gid: int, uid: int, min_threshold: int) -> tuple[bool, int, str]:
    total, name = _get_total_and_level(db_path, gid, uid)
    return (total >= min_threshold), total, name


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11  # handle soft/hard via hand_value()
    return int(rank)


def hand_value(cards: List[str]) -> int:
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10  # downgrade an Ace from 11 -> 1
        aces -= 1
    return total


def format_cards(cards: List[str], hide_first: bool = False) -> str:
    if hide_first and cards:
        return "🂠 " + " ".join(f"`{c}`" for c in cards[1:])
    return " ".join(f"`{c}`" for c in cards)


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        # blackjack state: {(guild_id, user_id): {...}}
        self.bj_state: Dict[Tuple[int, int], dict] = {}

    # ---------- DB helpers ----------
    def _con(self):
        return sqlite3.connect(self.db_path)

    def _get_currency_symbol(self, guild_id: int) -> str:
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT currency_symbol FROM guild_settings WHERE guild_id=?", (guild_id,))
        row = cur.fetchone()
        con.close()
        return row[0] if row and row[0] else DEFAULT_SYMBOL

    def _fmt_money(self, guild_id: int, amount: int) -> str:
        return f"{self._get_currency_symbol(guild_id)}{amount}"

    def _ensure_coins_row(self, guild_id: int, user_id: int) -> None:
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM coins WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if not cur.fetchone():
            cur.execute("INSERT INTO coins (guild_id, user_id, balance, last_claim) VALUES (?, ?, 0, 0)",
                        (guild_id, user_id))
            con.commit()
        con.close()

    def _get_balance(self, guild_id: int, user_id: int) -> int:
        self._ensure_coins_row(guild_id, user_id)
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT balance FROM coins WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = cur.fetchone()
        con.close()
        return int(row[0]) if row else 0

    def _set_balance(self, guild_id: int, user_id: int, new_bal: int) -> None:
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO coins (guild_id, user_id, balance, last_claim)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=excluded.balance
        """, (guild_id, user_id, int(new_bal)))
        con.commit()
        con.close()

    def _add_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        bal = self._get_balance(guild_id, user_id)
        new_bal = max(0, bal + int(delta))
        self._set_balance(guild_id, user_id, new_bal)
        return new_bal

    # ---------- Blackjack ----------
    def _new_deck(self) -> List[str]:
        ranks = ["A"] + [str(n) for n in range(2, 11)] + ["J", "Q", "K"]
        suits = ["♠", "♥", "♦", "♣"]
        deck = [f"{r}{s}" for r in ranks for s in suits]
        random.shuffle(deck)
        return deck

    def _bj_key(self, ctx: commands.Context) -> Tuple[int, int]:
        return (ctx.guild.id, ctx.author.id)

    def _bj_state(self, ctx: commands.Context) -> dict | None:
        return self.bj_state.get(self._bj_key(ctx))

    def _bj_end(self, ctx: commands.Context) -> None:
        self.bj_state.pop(self._bj_key(ctx), None)

    async def _bj_show(self, ctx: commands.Context, reveal_dealer: bool = False, footer: str = ""):
        st = self._bj_state(ctx)
        if not st:
            return
        p, d = st["player"], st["dealer"]
        pv = hand_value(p)
        dv = hand_value(d) if reveal_dealer else "?"
        embed = discord.Embed(
            title=f"🃏 Blackjack — Bet {self._fmt_money(ctx.guild.id, st['bet'])}",
            color=discord.Color.blurple()
        )
        embed.add_field(name=f"Your hand ({pv})", value=format_cards(p), inline=False)
        embed.add_field(
            name=f"Dealer ({dv})",
            value=format_cards(d, hide_first=(not reveal_dealer)),
            inline=False
        )
        if footer:
            embed.set_footer(text=footer)
        await ctx.reply(embed=embed, mention_author=False)

    def _bj_settle(self, gid: int, uid: int, bet: int, result: str) -> int:
        """
        result = 'win', 'blackjack', 'lose', 'push', 'surrender'
        Returns new balance.
        """
        if result == "win":
            return self._add_balance(gid, uid, bet * 2)      # return 2x (profit + bet)
        if result == "blackjack":
            return self._add_balance(gid, uid, int(bet * 2.5))  # 3:2 payout
        if result == "push":
            return self._add_balance(gid, uid, bet)          # return bet
        if result == "surrender":
            return self._add_balance(gid, uid, bet // 2)     # refund half
        # lose -> nothing back
        return self._get_balance(gid, uid)

    def _dealer_play(self, deck: List[str], dealer: List[str]) -> None:
        # Dealer stands on 17 (including soft 17 for simplicity)
        while hand_value(dealer) < 17:
            dealer.append(deck.pop())

    # Start game
    @commands.command(name="blackjack")
    async def blackjack(self, ctx: commands.Context, bet: int):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 10)  # LV2
        if not ok:
            return await ctx.reply(
                f"You must be **LV2** to play games. You’re **{lvl}** with `{total}` messages. Keep chatting!",
                mention_author=False
            )
        
        """
        Start a blackjack round with <bet>.
        Then use: !hit, !stand, !double, !surrender
        """
        if bet <= 0:
            return await ctx.reply("Bet must be a positive integer.", mention_author=False)

        gid, uid = ctx.guild.id, ctx.author.id
        if self._bj_state(ctx):
            return await ctx.reply("You already have an active blackjack hand. Use `!hit`, `!stand`, `!double`, or `!surrender`.", mention_author=False)

        bal = self._get_balance(gid, uid)
        if bal < bet:
            return await ctx.reply(
                f"Insufficient funds. Your balance is {self._fmt_money(gid, bal)}.",
                mention_author=False
            )

        # Deduct bet up front
        self._add_balance(gid, uid, -bet)

        deck = self._new_deck()
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]

        st = {"bet": bet, "deck": deck, "player": player, "dealer": dealer, "done": False}
        self.bj_state[self._bj_key(ctx)] = st

        pv, dv = hand_value(player), hand_value(dealer)

        # Natural checks
        player_bj = (pv == 21)
        dealer_bj = (dv == 21)

        if player_bj or dealer_bj:
            footer = ""
            result = None
            if player_bj and not dealer_bj:
                result = "blackjack"; footer = "Blackjack! You win 3:2."
            elif dealer_bj and not player_bj:
                result = "lose"; footer = "Dealer has blackjack. You lose."
            else:
                result = "push"; footer = "Both have blackjack. Push."

            new_bal = self._bj_settle(gid, uid, bet, result)
            await self._bj_show(ctx, reveal_dealer=True, footer=f"{footer} New balance: {self._fmt_money(gid, new_bal)}")
            self._bj_end(ctx)
            return

        await self._bj_show(ctx, reveal_dealer=False, footer="Your move: !hit, !stand, !double, !surrender")

    # Actions
    @commands.command(name="hit")
    async def bj_hit(self, ctx: commands.Context):
        st = self._bj_state(ctx)
        if not st:
            return await ctx.reply("No active blackjack hand. Start with `!blackjack <bet>`.", mention_author=False)

        st["player"].append(st["deck"].pop())
        pv = hand_value(st["player"])
        if pv > 21:
            # bust -> dealer wins
            new_bal = self._bj_settle(ctx.guild.id, ctx.author.id, st["bet"], "lose")
            await self._bj_show(ctx, reveal_dealer=True, footer=f"You bust. You lose. New balance: {self._fmt_money(ctx.guild.id, new_bal)}")
            self._bj_end(ctx)
            return

        await self._bj_show(ctx, reveal_dealer=False, footer="Your move: !hit, !stand, !double, !surrender")

    @commands.command(name="stand")
    async def bj_stand(self, ctx: commands.Context):
        st = self._bj_state(ctx)
        if not st:
            return await ctx.reply("No active blackjack hand.", mention_author=False)

        self._dealer_play(st["deck"], st["dealer"])
        pv, dv = hand_value(st["player"]), hand_value(st["dealer"])

        if dv > 21 or pv > dv:
            res, msg = "win", "You win!"
        elif pv < dv:
            res, msg = "lose", "Dealer wins."
        else:
            res, msg = "push", "Push."

        new_bal = self._bj_settle(ctx.guild.id, ctx.author.id, st["bet"], res)
        await self._bj_show(ctx, reveal_dealer=True, footer=f"{msg} New balance: {self._fmt_money(ctx.guild.id, new_bal)}")
        self._bj_end(ctx)

    @commands.command(name="double", aliases=["doubledown"])
    async def bj_double(self, ctx: commands.Context):
        st = self._bj_state(ctx)
        if not st:
            return await ctx.reply("No active blackjack hand.", mention_author=False)

        gid, uid = ctx.guild.id, ctx.author.id
        bet = st["bet"]
        bal = self._get_balance(gid, uid)
        if bal < bet:
            return await ctx.reply(f"You need {self._fmt_money(gid, bet)} available to double down.", mention_author=False)

        # deduct additional bet
        self._add_balance(gid, uid, -bet)
        st["bet"] += bet

        # draw one card and stand
        st["player"].append(st["deck"].pop())
        pv = hand_value(st["player"])

        if pv > 21:
            new_bal = self._bj_settle(gid, uid, st["bet"], "lose")
            await self._bj_show(ctx, reveal_dealer=True, footer=f"You bust after doubling. New balance: {self._fmt_money(gid, new_bal)}")
            self._bj_end(ctx)
            return

        # dealer plays
        self._dealer_play(st["deck"], st["dealer"])
        dv = hand_value(st["dealer"])
        if dv > 21 or pv > dv:
            res, msg = "win", "You win!"
        elif pv < dv:
            res, msg = "lose", "Dealer wins."
        else:
            res, msg = "push", "Push."

        new_bal = self._bj_settle(gid, uid, st["bet"], res)
        await self._bj_show(ctx, reveal_dealer=True, footer=f"{msg} New balance: {self._fmt_money(gid, new_bal)}")
        self._bj_end(ctx)

    @commands.command(name="surrender")
    async def bj_surrender(self, ctx: commands.Context):
        st = self._bj_state(ctx)
        if not st:
            return await ctx.reply("No active blackjack hand.", mention_author=False)
        new_bal = self._bj_settle(ctx.guild.id, ctx.author.id, st["bet"], "surrender")
        await self._bj_show(ctx, reveal_dealer=True, footer=f"You surrendered. Refunded half your bet. New balance: {self._fmt_money(ctx.guild.id, new_bal)}")
        self._bj_end(ctx)

    # ---------- Dice Roll ----------
    @commands.command(name="diceroll", aliases=["dice"])
    async def diceroll(self, ctx: commands.Context, bet: int):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 10)  # LV2
        if not ok:
            return await ctx.reply(
                f"You must be **LV2** to play games. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Bet 10, 50, or 100; then choose a face (1-6). Rolls 2 dice.
        One match pays x3, double match pays x10. Bet deducted up front.
        Usage: !diceroll 50
        """
        if bet not in (10, 50, 100):
            return await ctx.reply("Bet must be 10, 50, or 100.", mention_author=False)

        gid, uid = ctx.guild.id, ctx.author.id
        bal = self._get_balance(gid, uid)
        if bal < bet:
            return await ctx.reply(
                f"Insufficient funds. Your balance is {self._fmt_money(gid, bal)}.",
                mention_author=False
            )

        # deduct bet
        self._add_balance(gid, uid, -bet)

        await ctx.reply(
            f"🎲 Pick a face **1-6** for your bet of {self._fmt_money(gid, bet)}. "
            f"Reply with a number within 20 seconds.",
            mention_author=False
        )

        def check(m: discord.Message):
            return m.author.id == uid and m.channel.id == ctx.channel.id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=20.0)
        except Exception:
            # refund on timeout
            self._add_balance(gid, uid, bet)
            return await ctx.send("⏳ Timed out. Bet refunded.")

        try:
            face = int(msg.content.strip())
        except ValueError:
            self._add_balance(gid, uid, bet)
            return await ctx.send("Invalid number. Bet refunded.")

        if face < 1 or face > 6:
            self._add_balance(gid, uid, bet)
            return await ctx.send("Face must be between 1 and 6. Bet refunded.")

        roll1, roll2 = random.randint(1, 6), random.randint(1, 6)
        matches = (1 if roll1 == face else 0) + (1 if roll2 == face else 0)

        symbol = self._get_currency_symbol(gid)
        if matches == 2:
            payout = bet * 10
            new_bal = self._add_balance(gid, uid, payout)
            desc = f"🎉 Double match! Rolled **{roll1}** and **{roll2}**. You win **{symbol}{payout}**."
        elif matches == 1:
            payout = bet * 3
            new_bal = self._add_balance(gid, uid, payout)
            desc = f"✅ One match! Rolled **{roll1}** and **{roll2}**. You win **{symbol}{payout}**."
        else:
            new_bal = self._get_balance(gid, uid)
            desc = f"❌ No match. Rolled **{roll1}** and **{roll2}**. Better luck next time."

        await ctx.reply(f"{desc}\nNew balance: **{self._fmt_money(gid, new_bal)}**.", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))