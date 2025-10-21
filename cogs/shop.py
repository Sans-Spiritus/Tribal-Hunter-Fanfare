# cogs/shop.py
import os
import sqlite3
from typing import List, Tuple
from cogs.utils.economy_adapter import EconomyAdapter
from cogs.utils.levels import _meets_level

import discord
from discord.ext import commands

DB_PATH = os.getenv("DB_PATH", "levels.db")

DEFAULT_SYMBOL = "🪙"  # keep near top with other defaults. Used in money+coin helpers.

# Deltarune-ish pocket look
DEFAULT_SLOTS = 12         # total slots shown (6 rows x 2 cols)
ROWS = 6
COLS = 2
CELL_WIDTH = 16            # width for each item cell (mono font)
EMPTY_TEXT = "--"          # how an empty slot is shown


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH

    # ---------- DB ----------
    def _con(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        con = self._con()
        cur = con.cursor()

        # Configurable items per guild
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shop_items (
                item_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                name        TEXT NOT NULL,
                emoji       TEXT,
                description TEXT,
                price       INTEGER NOT NULL DEFAULT 0,
                max_stack   INTEGER NOT NULL DEFAULT 99,
                grants_role_id INTEGER,                 -- NEW
                is_listed   INTEGER NOT NULL DEFAULT 0, -- NEW (0 = hidden; !shopadd will list)
                UNIQUE(guild_id, name)
            )
        """)

        # user inventory
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_inventory (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                item_id  INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, item_id)
            )
        """)

        # MIGRATIONS
        cur.execute("PRAGMA table_info(shop_items)")
        cols = {r[1] for r in cur.fetchall()}
        if "grants_role_id" not in cols:
            cur.execute("ALTER TABLE shop_items ADD COLUMN grants_role_id INTEGER")
        if "is_listed" not in cols:
            cur.execute("ALTER TABLE shop_items ADD COLUMN is_listed INTEGER NOT NULL DEFAULT 0")
        if "display_order" not in cols:
            cur.execute("ALTER TABLE shop_items ADD COLUMN display_order INTEGER")

        con.commit()
        con.close()

        # Assign display_order where missing
        self._normalize_display_order_all_guilds()

    def _normalize_display_order_all_guilds(self):
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT DISTINCT guild_id FROM shop_items")
        gids = [r[0] for r in cur.fetchall()]
        con.close()
        for gid in gids:
            self._normalize_display_order(gid)

    def _normalize_display_order(self, guild_id: int):
        """Ensure listed items have contiguous display_order starting from 1."""
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT item_id FROM shop_items
            WHERE guild_id=? AND is_listed=1
            ORDER BY 
                CASE WHEN display_order IS NULL THEN 1 ELSE 0 END ASC,
                display_order ASC,
                price ASC, name COLLATE NOCASE ASC
        """, (guild_id,))
        rows = cur.fetchall()
        order = 1
        for (item_id,) in rows:
            cur.execute("UPDATE shop_items SET display_order=? WHERE guild_id=? AND item_id=?",
                        (order, guild_id, item_id))
            order += 1
        con.commit()
        con.close()

    def _next_display_order(self, guild_id: int) -> int:
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT COALESCE(MAX(display_order), 0) FROM shop_items WHERE guild_id=? AND is_listed=1", (guild_id,))
        n = int(cur.fetchone()[0] or 0)
        con.close()
        return n + 1

    @commands.Cog.listener()
    async def on_ready(self):
        self._init_db()

    # ---------- helpers ----------
    def _get_inventory_rows(self, guild_id: int, user_id: int) -> List[Tuple[str, int, str]]:
        """
        Return list of (name, quantity, emoji) sorted by name.
        """
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT si.name, ui.quantity, COALESCE(si.emoji, '')
            FROM user_inventory ui
            JOIN shop_items si ON si.item_id = ui.item_id
            WHERE ui.guild_id=? AND ui.user_id=? AND ui.quantity > 0
            ORDER BY si.name COLLATE NOCASE ASC
        """, (guild_id, user_id))
        rows = cur.fetchall()
        con.close()
        return [(str(n), int(q), str(e)) for (n, q, e) in rows]

    def _ensure_item(self, guild_id: int, name: str, emoji: str | None = None, price: int = 0, max_stack: int = 99) -> int:
        """
        Get or create an item; returns item_id.
        """
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT item_id FROM shop_items WHERE guild_id=? AND name=?", (guild_id, name))
        row = cur.fetchone()
        if row:
            item_id = int(row[0])
        else:
            cur.execute("""
                INSERT INTO shop_items (guild_id, name, emoji, description, price, max_stack)
                VALUES (?, ?, ?, '', ?, ?)
            """, (guild_id, name, emoji or "", price, max_stack))
            item_id = cur.lastrowid
            con.commit()
        con.close()
        return item_id

    def _add_inventory(self, guild_id: int, user_id: int, item_id: int, qty: int):
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO user_inventory (guild_id, user_id, item_id, quantity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, item_id)
            DO UPDATE SET quantity = quantity + excluded.quantity
        """, (guild_id, user_id, item_id, qty))
        con.commit()
        con.close()

    def _get_item_by_id(self, guild_id: int, item_id: int):
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT item_id, name, COALESCE(emoji,''), description, price, max_stack, grants_role_id, is_listed
            FROM shop_items
            WHERE guild_id=? AND item_id=?
        """, (guild_id, item_id))
        row = cur.fetchone()
        con.close()
        if not row:
            return None
        keys = ["item_id","name","emoji","description","price","max_stack","grants_role_id","is_listed"]
        return dict(zip(keys, row))

    def _get_user_item_quantity(self, guild_id: int, user_id: int, item_id: int) -> int:
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT quantity FROM user_inventory
            WHERE guild_id=? AND user_id=? AND item_id=?
        """, (guild_id, user_id, item_id))
        row = cur.fetchone()
        con.close()
        return int(row[0]) if row else 0
    
    def _consume_item_once(self, guild_id: int, user_id: int, item_id: int) -> tuple[bool, int]:
        """
        Try to consume (decrement) one item.
        Returns (consumed, new_qty).
        """
        con = self._con()
        cur = con.cursor()
        # fetch current
        cur.execute("""
            SELECT quantity FROM user_inventory
            WHERE guild_id=? AND user_id=? AND item_id=?
        """, (guild_id, user_id, item_id))
        row = cur.fetchone()
        current = int(row[0]) if row else 0
        if current <= 0:
            con.close()
            return False, 0
        new_qty = current - 1
        cur.execute("""
            UPDATE user_inventory
            SET quantity=?
            WHERE guild_id=? AND user_id=? AND item_id=?
        """, (new_qty, guild_id, user_id, item_id))
        con.commit()
        con.close()
        return True, new_qty
    
    def _remove_inventory(self, guild_id: int, user_id: int, item_id: int, qty: int) -> tuple[int, int]:
        """
        Remove up to qty from the user's inventory.
        Returns (actually_removed, new_quantity).
        """
        qty = max(0, int(qty))
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT quantity FROM user_inventory
            WHERE guild_id=? AND user_id=? AND item_id=?
        """, (guild_id, user_id, item_id))
        row = cur.fetchone()
        current = int(row[0]) if row else 0
        if current <= 0 or qty <= 0:
            con.close()
            return 0, current
        removed = min(qty, current)
        new_qty = current - removed
        cur.execute("""
            UPDATE user_inventory SET quantity=?
            WHERE guild_id=? AND user_id=? AND item_id=?
        """, (new_qty, guild_id, user_id, item_id))
        con.commit()
        con.close()
        return removed, new_qty

    # ------- money & coins helpers (use same DB as coins cog) -------
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
            cur.execute("INSERT INTO coins (guild_id, user_id, balance, last_claim) VALUES (?, ?, 0, 0)", (guild_id, user_id))
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

    def _add_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        bal = self._get_balance(guild_id, user_id)
        new_bal = max(0, bal + int(delta))
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO coins (guild_id, user_id, balance, last_claim)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET balance=excluded.balance
        """, (guild_id, user_id, new_bal))
        con.commit()
        con.close()
        return new_bal
    
    def _get_item_by_name(self, guild_id: int, name: str):
        """Return dict with item fields or None (case-insensitive by name)."""
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT item_id, name, emoji, description, price, max_stack, grants_role_id, is_listed
            FROM shop_items
            WHERE guild_id=? AND LOWER(name)=LOWER(?)
        """, (guild_id, name))
        row = cur.fetchone()
        con.close()
        if not row:
            return None
        keys = ["item_id","name","emoji","description","price","max_stack","grants_role_id","is_listed"]
        return dict(zip(keys, row))

    # Format a single cell to a fixed width (mono), adding quantity/emoji and truncating long names
    def _fmt_cell(self, name: str | None, qty: int = 0, emoji: str = "") -> str:
        if not name:  # empty slot
            content = EMPTY_TEXT
        else:
            base = f"{emoji}{name}".strip()
            # append quantity (xN) if more than 1
            if qty > 1:
                base = f"{base} x{qty}"
            # truncate and pad to fixed width
            if len(base) > CELL_WIDTH:
                base = base[:CELL_WIDTH-1] + "…"  # ellipsis
            content = base
        return content.ljust(CELL_WIDTH)

    def _build_grid(self, items: List[Tuple[str, int, str]], slots: int = DEFAULT_SLOTS) -> str:
        """
        Build a two-column Deltarune-like grid in a code block (monospace).
        """
        # take up to 'slots' items; pad the rest
        entries = items[:slots]
        while len(entries) < slots:
            entries.append((None, 0, ""))  # empty

        # split into rows/cols
        left_col = entries[0:ROWS]
        right_col = entries[ROWS:ROWS*2]

        lines = []
        # Header line (like POCKET)
        lines.append("POCKET".ljust(CELL_WIDTH) + " " * 3 + "".ljust(CELL_WIDTH))
        # Grid content
        for r in range(ROWS):
            l_name, l_qty, l_emoji = left_col[r]
            r_name, r_qty, r_emoji = right_col[r]
            left = self._fmt_cell(l_name, l_qty, l_emoji)
            right = self._fmt_cell(r_name, r_qty, r_emoji)
            lines.append(f"{left}   {right}")

        return "```\n" + "\n".join(lines) + "\n```"
    
    def _create_item_full(
        self,
        guild_id: int,
        name: str,
        description: str,
        emoji: str | None = "",
        price: int = 0,
        max_stack: int = 99,
        grants_role_id: int | None = None,
        is_listed: int = 0
    ) -> int:
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            INSERT INTO shop_items (guild_id, name, emoji, description, price, max_stack, grants_role_id, is_listed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, name) DO UPDATE SET
            emoji=excluded.emoji,
            description=excluded.description,
            price=excluded.price,
            max_stack=excluded.max_stack,
            grants_role_id=excluded.grants_role_id,
            is_listed=excluded.is_listed
        """, (guild_id, name, emoji or "", description, int(price), int(max_stack),
            grants_role_id if grants_role_id else None, int(is_listed)))
        item_id = cur.lastrowid
        con.commit()
        con.close()
        return item_id
    
    async def _ask(self, ctx: commands.Context, prompt: str, *, timeout: int = 120):
        """Ask the invoking user a question in the same channel; return content or None if quit/timeout."""
        await ctx.send(prompt)
        def check(m: discord.Message):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=timeout)
        except Exception:
            await ctx.send("⏳ Timed out. Creation aborted.")
            return None
        content = msg.content.strip()
        if content.lower() == "quit":
            await ctx.send("❌ Aborted.")
            return None
        return content

    # ---------- commands ----------
    @commands.command(name="inventory", aliases=["inv", "pocket", "bag"])
    async def inventory(self, ctx: commands.Context, member: discord.Member | None = None):
        """
        Show your inventory in a Deltarune-like grid (6x2).
        """
        target = member or ctx.author
        rows = self._get_inventory_rows(ctx.guild.id, target.id)

        if not rows:
            # still show an empty pocket
            grid = self._build_grid([])
        else:
            grid = self._build_grid(rows)

        embed = discord.Embed(
            title=f"{target.display_name}'s Inventory",
            description=grid,
            color=discord.Color.dark_gray()
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)

        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="itemcreate")
    @commands.has_permissions(manage_roles=True)
    async def itemcreate(self, ctx: commands.Context):
        """
        Start an interactive wizard to create an item.
        Flow: Name -> Description -> (Y/N) role grant -> Role ID (or 0)
        """
        gid = ctx.guild.id

        # 1) NAME
        name = await self._ask(ctx, "🛒 What is the **name** of the item?\nType `quit` to abort.")
        if not name:
            return
        if len(name) > 64:
            return await ctx.reply("Name too long (max 64). Aborted.", mention_author=False)

        # Check uniqueness
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM shop_items WHERE guild_id=? AND name=?", (gid, name))
        if cur.fetchone():
            con.close()
            return await ctx.reply("An item with that name already exists in this server. Aborted.", mention_author=False)
        con.close()

        # 2) DESCRIPTION
        desc = await self._ask(ctx, f"✏️ What is the **description** of **{name}**?\nType `quit` to abort.")
        if desc is None:
            return
        if len(desc) > 512:
            return await ctx.reply("Description too long (max 512). Aborted.", mention_author=False)

        # 3) ROLE GRANT? (Y/N)
        yn = await self._ask(ctx, f"🎁 Should **{name}** grant a role when used? (Y/N)\nType `quit` to abort.")
        if yn is None:
            return
        yn = yn.lower()
        grants_role_id: int | None = None
        if yn in ("y", "yes"):
            # 4) ROLE ID
            rid_text = await self._ask(
                ctx,
                "🔑 Enter the **Role ID** (paste the numeric ID). Type `0` to cancel this step."
            )
            if rid_text is None:
                return
            rid_text = rid_text.strip()
            if rid_text != "0":
                # accept mention or raw ID
                role: discord.Role | None = None
                # try parser
                try:
                    role = await commands.RoleConverter().convert(ctx, rid_text)
                except commands.BadArgument:
                    # try numeric fallback
                    try:
                        role = ctx.guild.get_role(int(rid_text))
                    except Exception:
                        role = None
                if not role:
                    return await ctx.reply("Couldn’t resolve that role. Aborted.", mention_author=False)
                grants_role_id = role.id

        # Create item with defaults; price/listing handled later by !shopadd
        self._create_item_full(
            guild_id=gid,
            name=name,
            description=desc,
            emoji="",
            price=0,
            max_stack=99,
            grants_role_id=grants_role_id,
            is_listed=0
        )

        lines = [
            "✅ **Item created!**",
            f"**Name:** {name}",
            f"**Description:** {desc}",
            f"**Grants Role:** {f'<@&{grants_role_id}>' if grants_role_id else 'No'}",
            "Next: add it to the shop with `!shopadd` (we’ll wire that next)."
        ]
        await ctx.reply("\n".join(lines), allowed_mentions=discord.AllowedMentions.none(), mention_author=False)

    @commands.command(name="shopadd")
    @commands.has_permissions(manage_roles=True)
    async def shopadd(self, ctx: commands.Context, item_name: str, price: int):
        """
        List an existing item for sale and set its price.
        Usage: !shopadd "Alpha Plushie" 250
        (Quotes required if the name has spaces.)
        """
        if price < 0:
            return await ctx.reply("Price must be non-negative.", mention_author=False)

        gid = ctx.guild.id
        it = self._get_item_by_name(gid, item_name)
        if not it:
            return await ctx.reply("No item with that name exists. Create it first with `!itemcreate`.", mention_author=False)

        order = self._next_display_order(gid)
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            UPDATE shop_items SET price=?, is_listed=1,
                display_order=COALESCE(display_order, ?)
            WHERE guild_id=? AND item_id=?
        """, (price, order, gid, it["item_id"]))
        con.commit()
        con.close()
        self._normalize_display_order(gid)

        await ctx.reply(
            f"✅ Listed **{it['name']}** for **{self._fmt_money(gid, price)}**. "
            f"Use `!shop` to view.",
            mention_author=False
        )

    @commands.command(name="shop")
    async def shop(self, ctx: commands.Context, page: int = 1):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 100)  # LV3
        if not ok:
            return await ctx.reply(
                f"You must be **LV3** to use the shop. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Show items currently for sale. Default page 1. 10 items per page.
        """
        gid = ctx.guild.id
        page = max(1, page)
        page_size = 10
        offset = (page - 1) * page_size

        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT name, price, COALESCE(emoji,''), description
            FROM shop_items
            WHERE guild_id=? AND is_listed=1
            ORDER BY display_order ASC, name COLLATE NOCASE ASC
            LIMIT ? OFFSET ?
        """, (gid, page_size, offset))
        # count unchanged

        rows = cur.fetchall()  # <-- add this line so rows is defined

        # count total for pages
        cur.execute("SELECT COUNT(*) FROM shop_items WHERE guild_id=? AND is_listed=1", (gid,))
        total = cur.fetchone()[0]
        con.close()

        if not rows:
            return await ctx.reply("The shop is empty. Ask a mod to add items with `!shopadd`.", mention_author=False)

        symbol = self._get_currency_symbol(gid)
        desc_lines = []
        start = offset + 1
        for idx, (name, price, emoji, description) in enumerate(rows, start=start):
            line = f"**#{idx}** {emoji}{name} — **{symbol}{price}**"
            if description:
                line += f"\n> {description}"
            desc_lines.append(line)

        max_pages = (total + page_size - 1) // page_size
        embed = discord.Embed(
            title=f"🛒 Shop (Page {page}/{max_pages})",
            description="\n\n".join(desc_lines),
            color=discord.Color.green()
        )
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, item_name: str, qty: int = 1):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 100)  # LV3
        if not ok:
            return await ctx.reply(
                f"You must be **LV3** to buy items. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Buy a listed item. Quotes required if the name has spaces.
        Usage: !buy "Alpha Plushie" 2
        """
        gid = ctx.guild.id
        uid = ctx.author.id

        if qty <= 0:
            return await ctx.reply("Quantity must be a positive integer.", mention_author=False)

        it = self._get_item_by_name(gid, item_name)
        if not it or not it["is_listed"]:
            return await ctx.reply("That item isn’t for sale.", mention_author=False)

        price = int(it["price"])
        total_cost = price * qty
        bal = self._get_balance(gid, uid)

        if bal < total_cost:
            return await ctx.reply(
                f"Not enough funds. Price: **{self._fmt_money(gid, total_cost)}**, "
                f"your balance: **{self._fmt_money(gid, bal)}**.",
                mention_author=False
            )

        # Deduct and grant
        new_bal = self._add_balance(gid, uid, -total_cost)
        self._add_inventory(gid, uid, it["item_id"], qty)

        await ctx.reply(
            f"✅ Purchased **{qty}× {it['name']}** for **{self._fmt_money(gid, total_cost)}**.\n"
            f"New balance: **{self._fmt_money(gid, new_bal)}**. "
            f"Check your inventory with `!inventory`.",
            mention_author=False
        )

    @commands.command(name="use")
    async def use_item(self, ctx: commands.Context, *, item_query: str):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 100)  # LV3
        if not ok:
            return await ctx.reply(
                f"You must be **LV3** to use items. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Use an item from your inventory.
        - If the item grants a role, the bot will assign it (and ONLY consume on success).
        - Otherwise, it just consumes one and shows a flavor message.
        Examples:
        !use Dark Candy
        !use 42
        """
        gid = ctx.guild.id
        uid = ctx.author.id
        member = ctx.author

        # Resolve by ID or by name
        item = self._get_item_by_id(gid, int(item_query)) if item_query.isdigit() else None
        if not item:
            item = self._get_item_by_name(gid, item_query)
        if not item:
            return await ctx.reply("I can’t find that item.", mention_author=False)

        qty = self._get_user_item_quantity(gid, uid, item["item_id"])
        if qty <= 0:
            return await ctx.reply(f"You don’t have any **{item['name']}**.", mention_author=False)

        # If item grants a role, try to give it — only consume on success
        role_id = item.get("grants_role_id")
        if role_id:
            role = ctx.guild.get_role(int(role_id))
            if not role:
                return await ctx.reply(
                    "This item is supposed to grant a role, but that role no longer exists. "
                    "Please tell a moderator.", mention_author=False
                )
            if role in member.roles:
                return await ctx.reply(
                    f"You already have **{role.name}**. The item wasn’t consumed.",
                    mention_author=False
                )
            # try to assign role
            try:
                await member.add_roles(role, reason=f"Used item: {item['name']}")
            except discord.Forbidden:
                return await ctx.reply(
                    "I don’t have permission to give that role (check **Manage Roles** and role position). "
                    "The item wasn’t consumed.", mention_author=False
                )
            except discord.HTTPException:
                return await ctx.reply(
                    "Discord API error while assigning the role. The item wasn’t consumed.",
                    mention_author=False
                )

            # role assignment succeeded -> consume one
            consumed, new_qty = self._consume_item_once(gid, uid, item["item_id"])
            if not consumed:
                # extremely unlikely race—remove role we just gave to keep state consistent
                try:
                    await member.remove_roles(role, reason="Reverting: failed to consume item")
                except Exception:
                    pass
                return await ctx.reply("Something went wrong consuming the item. Try again.", mention_author=False)

            return await ctx.reply(
                f"✅ You used **{item['name']}**.\n"
                f"🎁 Granted role: **{role.name}**.\n"
                f"Remaining: **x{new_qty}**.",
                mention_author=False
            )

        # Non-role items: consume and show a flavor message
        consumed, new_qty = self._consume_item_once(gid, uid, item["item_id"])
        if not consumed:
            return await ctx.reply("You don’t have that item.", mention_author=False)

        await ctx.reply(
            f"✅ You used **{item['name']}**. (No special effect… yet!)\n"
            f"Remaining: **x{new_qty}**.",
            mention_author=False
        )

    @commands.command(name="info", aliases=["iteminfo"])
    async def info(self, ctx: commands.Context, *, item_query: str):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 100)  # LV3
        if not ok:
            return await ctx.reply(
                f"You must be **LV3** to view item info. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Show item details (price, description, max stack, role grant, your quantity).
        Works with names or numeric IDs.
        """
        gid = ctx.guild.id
        uid = ctx.author.id

        item = self._get_item_by_id(gid, int(item_query)) if item_query.isdigit() else None
        if not item:
            item = self._get_item_by_name(gid, item_query)
        if not item:
            return await ctx.reply("I can’t find that item.", mention_author=False)

        qty = self._get_user_item_quantity(gid, uid, item["item_id"])
        symbol = self._get_currency_symbol(gid)

        role_text = f"<@&{item['grants_role_id']}>" if item.get("grants_role_id") else "—"
        listed_text = "Yes" if item.get("is_listed") else "No"
        max_stack = item.get("max_stack") if item.get("max_stack") is not None else 99
        emoji = item.get("emoji") or ""

        embed = discord.Embed(
            title=f"{emoji}{item['name']}",
            description=(f"> {item['description']}" if item.get("description") else ""),
            color=discord.Color.teal()
        )
        embed.add_field(name="Price", value=f"**{symbol}{item['price']}**", inline=True)
        embed.add_field(name="In Shop", value=listed_text, inline=True)
        embed.add_field(name="Max Stack", value=f"x{max_stack}", inline=True)
        embed.add_field(name="Grants Role", value=role_text, inline=True)
        embed.add_field(name="You Own", value=f"x{qty}", inline=True)

        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="toss", aliases=["discard", "drop"])
    async def toss(self, ctx: commands.Context, *, args: str):
        ok, total, lvl = _meets_level(self.db_path, ctx.guild.id, ctx.author.id, 100)  # LV3
        if not ok:
            return await ctx.reply(
                f"You must be **LV3** to toss items. You’re **{lvl}** with `{total}` messages.",
                mention_author=False
            )
        """
        Remove items from your inventory without using them.
        Usage:
        !toss Dark Candy 2
        !toss "Alpha Plushie"    (defaults to 1)
        !toss 42 3               (by item ID)
        """
        gid = ctx.guild.id
        uid = ctx.author.id

        parts = args.strip().split()
        if not parts:
            return await ctx.reply("Specify an item to toss.", mention_author=False)

        # If last token is an int, it's the amount; else default to 1.
        if parts[-1].isdigit():
            amount = int(parts[-1])
            item_query = " ".join(parts[:-1]).strip()
        else:
            amount = 1
            item_query = " ".join(parts).strip()

        if amount <= 0:
            return await ctx.reply("Amount must be a positive integer.", mention_author=False)
        if not item_query:
            return await ctx.reply("Specify an item name or ID.", mention_author=False)

        # Resolve item
        item = self._get_item_by_id(gid, int(item_query)) if item_query.isdigit() else None
        if not item:
            item = self._get_item_by_name(gid, item_query)
        if not item:
            return await ctx.reply("I can’t find that item.", mention_author=False)

        have = self._get_user_item_quantity(gid, uid, item["item_id"])
        if have <= 0:
            return await ctx.reply(f"You don’t have any **{item['name']}**.", mention_author=False)

        removed, new_qty = self._remove_inventory(gid, uid, item["item_id"], amount)
        if removed <= 0:
            return await ctx.reply("Nothing was tossed.", mention_author=False)

        emoji = item.get("emoji") or ""
        note = ""
        if removed < amount:
            note = f" (requested {amount}, tossed {removed})"

        await ctx.reply(
            f"🗑️ You tossed **{removed}× {emoji}{item['name']}**.{note}\n"
            f"Remaining: **x{new_qty}**.",
            mention_author=False
        )

    @commands.command(name="itemedit")
    @commands.has_permissions(manage_roles=True)
    async def itemedit(self, ctx: commands.Context):
        """
        Wizard to edit an item (price/description/role) or delete it.
        """
        gid = ctx.guild.id

        # Pick item by shop # or name
        ans = await self._ask(ctx, "🛠️ Type the **shop #** or **item name** you wish to edit.\nType `quit` to abort.")
        if not ans:
            return

        item = None
        if ans.isdigit():
            # map shop # -> item
            idx = int(ans)
            con = self._con()
            cur = con.cursor()
            cur.execute("""
                SELECT item_id, name, price, COALESCE(emoji,''), COALESCE(description,''), COALESCE(grants_role_id,''), is_listed
                FROM shop_items
                WHERE guild_id=? AND is_listed=1
                ORDER BY display_order ASC, name COLLATE NOCASE ASC
                LIMIT 1 OFFSET ?
            """, (gid, max(0, idx-1)))
            row = cur.fetchone()
            con.close()
            if row:
                keys = ["item_id","name","price","emoji","description","grants_role_id","is_listed"]
                item = dict(zip(keys, row))
        if not item:
            item = self._get_item_by_name(gid, ans)

        if not item:
            return await ctx.reply("I couldn't find that item.", mention_author=False)

        # Price
        price_in = await self._ask(ctx, f"💰 New **price** for **{item['name']}**? Type `=` to keep the current price ({item['price']}).")
        if price_in is None:
            return
        if price_in.strip() == "=":
            new_price = int(item["price"])
        else:
            try:
                new_price = int(price_in)
                if new_price < 0: raise ValueError
            except ValueError:
                return await ctx.reply("Price must be a non-negative integer. Aborted.", mention_author=False)

        # Description
        desc_in = await self._ask(ctx, f"📝 New **description**? Type `=` to keep.")
        if desc_in is None:
            return
        if desc_in.strip() == "=":
            new_desc = item.get("description") or ""
        else:
            new_desc = desc_in.strip()
            if len(new_desc) > 512:
                return await ctx.reply("Description too long (max 512). Aborted.", mention_author=False)

        # Role
        role_in = await self._ask(ctx, "🎁 New **Role ID**? Type `=` to keep, or `None` to remove role assignment.")
        if role_in is None:
            return
        if role_in.strip().lower() == "none":
            new_role_id = None
        elif role_in.strip() == "=":
            new_role_id = item.get("grants_role_id")
        else:
            # allow mention or ID
            try:
                role = await commands.RoleConverter().convert(ctx, role_in.strip())
                new_role_id = role.id
            except commands.BadArgument:
                try:
                    new_role_id = int(role_in.strip())
                    if not ctx.guild.get_role(new_role_id):
                        return await ctx.reply("That role ID doesn't exist in this server. Aborted.", mention_author=False)
                except Exception:
                    return await ctx.reply("Couldn't parse that role. Aborted.", mention_author=False)

        # Confirm
        confirm = await self._ask(
            ctx,
            f"Review changes for **{item['name']}**:\n"
            f"• Price: {item['price']} → {new_price}\n"
            f"• Description: {('unchanged' if new_desc == (item.get('description') or '') else 'updated')}\n"
            f"• Grants Role: {item.get('grants_role_id') or 'None'} → {new_role_id or 'None'}\n\n"
            f"Type **Accept** to save, **Cancel** to revert, or **Delete** to delete the item permanently."
        )
        if not confirm:
            return
        c = confirm.strip().lower()
        if c == "cancel":
            return await ctx.reply("❌ Edit cancelled.", mention_author=False)
        elif c == "delete":
            # delete item + inventories referencing it
            con = self._con()
            cur = con.cursor()
            cur.execute("DELETE FROM user_inventory WHERE guild_id=? AND item_id=?", (gid, item["item_id"]))
            cur.execute("DELETE FROM shop_items WHERE guild_id=? AND item_id=?", (gid, item["item_id"]))
            con.commit()
            con.close()
            await ctx.reply("🗑️ Item deleted.", mention_author=False)
            # Re-pack display order
            self._normalize_display_order(gid)
            return
        elif c == "accept":
            con = self._con()
            cur = con.cursor()
            cur.execute("""
                UPDATE shop_items
                SET price=?, description=?, grants_role_id=?
                WHERE guild_id=? AND item_id=?
            """, (int(new_price), new_desc, new_role_id, gid, item["item_id"]))
            con.commit()
            con.close()
            await ctx.reply("✅ Item successfully edited!", mention_author=False)
            return
        else:
            return await ctx.reply("Did not recognize that response. Aborted.", mention_author=False)
        
    @commands.command(name="shopedit")
    @commands.has_permissions(manage_roles=True)
    async def shopedit(self, ctx: commands.Context):
        """
        Wizard to edit the shop listing by shop #:
        - swap: swap positions of two items
        - remove: unlist an item from the shop (keep the item in DB)
        """
        gid = ctx.guild.id
        self._normalize_display_order(gid)

        ans = await self._ask(ctx, "🛒 Type the **shop #** of the item you wish to edit.\nType `quit` to abort.")
        if not ans:
            return
        if not ans.isdigit():
            return await ctx.reply("Please provide a valid number.", mention_author=False)
        n1 = int(ans)

        # Fetch item at index n1
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            SELECT item_id, name, display_order FROM shop_items
            WHERE guild_id=? AND is_listed=1
            ORDER BY display_order ASC, name COLLATE NOCASE ASC
            LIMIT 1 OFFSET ?
        """, (gid, max(0, n1-1)))
        row = cur.fetchone()
        if not row:
            con.close()
            return await ctx.reply("That shop # doesn't exist.", mention_author=False)
        item1_id, item1_name, item1_order = row

        action = await self._ask(
            ctx,
            f"What do you want to do with **#{n1} {item1_name}**?\n"
            f"Type `swap` to swap positions, `remove` to remove from the shop, or `Cancel` to cancel."
        )
        if not action:
            con.close()
            return
        a = action.strip().lower()

        if a == "remove":
            cur.execute("UPDATE shop_items SET is_listed=0, display_order=NULL WHERE guild_id=? AND item_id=?", (gid, item1_id))
            con.commit()
            con.close()
            self._normalize_display_order(gid)
            return await ctx.reply(f"🗑️ **{item1_name}** removed from the shop (still exists as an item).", mention_author=False)

        if a != "swap":
            con.close()
            return await ctx.reply("Cancelled.", mention_author=False)

        # Swap flow
        ans2 = await self._ask(ctx, "Enter the **shop #** to swap with.")
        if not ans2 or not ans2.isdigit():
            con.close()
            return await ctx.reply("Cancelled.", mention_author=False)
        n2 = int(ans2)

        if n1 == n2:
            con.close()
            return await ctx.reply("Those are the same positions. Nothing to swap.", mention_author=False)

        cur.execute("""
            SELECT item_id, name, display_order FROM shop_items
            WHERE guild_id=? AND is_listed=1
            ORDER BY display_order ASC, name COLLATE NOCASE ASC
            LIMIT 1 OFFSET ?
        """, (gid, max(0, n2-1)))
        row2 = cur.fetchone()
        if not row2:
            con.close()
            return await ctx.reply("That destination # doesn't exist.", mention_author=False)
        item2_id, item2_name, item2_order = row2

        confirm = await self._ask(
            ctx,
            f"This will swap **#{n1} {item1_name}** with **#{n2} {item2_name}**. Is this okay? [Y/N]"
        )
        if not confirm or confirm.strip().lower() not in ("y", "yes"):
            con.close()
            return await ctx.reply("Cancelled.", mention_author=False)

        # Swap display_order
        cur.execute("UPDATE shop_items SET display_order=? WHERE guild_id=? AND item_id=?", (item2_order, gid, item1_id))
        cur.execute("UPDATE shop_items SET display_order=? WHERE guild_id=? AND item_id=?", (item1_order, gid, item2_id))
        con.commit()
        con.close()
        self._normalize_display_order(gid)

        await ctx.reply("✅ Items successfully swapped.", mention_author=False)

    # --- moderator helper to seed inventories for testing ---
    @commands.command(name="inv_give")
    @commands.has_permissions(manage_roles=True)
    async def inv_give(self, ctx: commands.Context, member: discord.Member, qty: int, *, item_query: str):
        """
        Give an existing item to a user by name or ID.
        Usage:
        !inv_give @User 2 Dark Candy
        !inv_give @User 1 42        (if you know the item_id)
        """
        if member.bot:
            return await ctx.reply("Bots don’t need items. 😉", mention_author=False)
        if qty <= 0:
            return await ctx.reply("Quantity must be a positive integer.", mention_author=False)

        gid = ctx.guild.id

        # Resolve by ID or by name (case-insensitive)
        item = None
        if item_query.isdigit():
            item = self._get_item_by_id(gid, int(item_query))
        if not item:
            item = self._get_item_by_name(gid, item_query)

        if not item:
            return await ctx.reply(
                "Item not found. Create it with `!itemcreate` (and `!shopadd` to list it) before giving.",
                mention_author=False
            )

        # Grant and show new total
        self._add_inventory(gid, member.id, item["item_id"], qty)
        new_qty = self._get_user_item_quantity(gid, member.id, item["item_id"])
        emoji = item["emoji"] or ""
        await ctx.reply(
            f"✅ Gave **{qty}× {emoji}{item['name']}** to **{member.display_name}**. "
            f"They now have **x{new_qty}**.",
            mention_author=False
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Shop(bot))
