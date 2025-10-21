import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "levels.db")

class EconomyAdapter:
    """Shared helper for all cogs that manipulate coins."""
    def __init__(self):
        self.db_path = DB_PATH

    def _con(self):
        return sqlite3.connect(self.db_path)

    def ensure_user(self, guild_id: int, user_id: int):
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM coins WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        if not cur.fetchone():
            cur.execute("INSERT INTO coins (guild_id, user_id, balance, last_claim) VALUES (?, ?, 0, 0)",
                        (guild_id, user_id))
            con.commit()
        con.close()

    def add_coins(self, guild_id: int, user_id: int, amount: int) -> int:
        """Add (or subtract) coins and return the new balance."""
        if amount == 0:
            return self.get_balance(guild_id, user_id)
        self.ensure_user(guild_id, user_id)
        con = self._con()
        cur = con.cursor()
        cur.execute("""
            UPDATE coins
            SET balance = MAX(0, balance + ?)
            WHERE guild_id=? AND user_id=?
        """, (amount, guild_id, user_id))
        con.commit()
        cur.execute("SELECT balance FROM coins WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        new_bal = cur.fetchone()[0]
        con.close()
        return int(new_bal)

    def get_balance(self, guild_id: int, user_id: int) -> int:
        """Return the user's balance (creates a row if missing)."""
        self.ensure_user(guild_id, user_id)
        con = self._con()
        cur = con.cursor()
        cur.execute("SELECT balance FROM coins WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        row = cur.fetchone()
        con.close()
        return int(row[0]) if row else 0
