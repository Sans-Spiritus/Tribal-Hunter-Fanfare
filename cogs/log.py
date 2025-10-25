# cogs/log.py
# Discord.py logging cog (discord.py 2.x)
# Logs: message create/edit/delete, reaction add/remove, member join/leave
# Command: !log <channel_id or #mention>   (clear with no args)
# Time format: 24h fixed EST (UTC-5; no DST)

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import discord
from discord.ext import commands

# ---- Fixed EST tz (no DST) ----
EST = timezone(timedelta(hours=-5), name="EST")

# ---- JSON config for per-guild log channel ----
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "log_config.json")


class GuildLogConfig:
    def __init__(self, channel_id: Optional[int] = None):
        self.channel_id = channel_id

    @classmethod
    def from_dict(cls, data: Dict) -> "GuildLogConfig":
        return cls(channel_id=data.get("channel_id"))

    def to_dict(self) -> Dict:
        return {"channel_id": self.channel_id}


class LoggerStore:
    """Tiny JSON-backed storage for per-guild logging channel IDs."""

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._configs: Dict[str, GuildLogConfig] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._configs = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._configs = {gid: GuildLogConfig.from_dict(cfg) for gid, cfg in data.items()}
        except Exception:
            self._configs = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = {gid: cfg.to_dict() for gid, cfg in self._configs.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(tmp, f, indent=2)

    def get(self, guild_id: int) -> GuildLogConfig:
        return self._configs.get(str(guild_id), GuildLogConfig())

    def set_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self._configs[str(guild_id)] = GuildLogConfig(channel_id)
        self._save()


class LogCog(commands.Cog, name="Log"):
    """Server logging cog with `!log` to set destination channel."""

    def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
        self.bot = bot
        self.store = LoggerStore()

    # ---------- utilities ----------

    @staticmethod
    def _now_text() -> str:
        return datetime.now(tz=EST).strftime("%H:%M:%S")

    @staticmethod
    def _safe_text(s: Optional[str], limit: int = 1200) -> str:
        if not s:
            return ""
        s_clean = s.replace("\n", "\\n")
        if len(s_clean) > limit:
            s_clean = s_clean[:limit] + "… (truncated)"
        return s_clean

    @staticmethod
    def _emoji_name(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
        if isinstance(emoji, str):
            return emoji
        return emoji.name or str(emoji)

    def _log_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
        if not guild:
            return None
        cfg = self.store.get(guild.id)
        if not cfg.channel_id:
            return None
        ch = guild.get_channel(cfg.channel_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _send_log(self, guild: Optional[discord.Guild], text: str) -> None:
        if not guild:
            return
        channel = self._log_channel(guild)
        if not channel:
            return
        try:
            payload = text if len(text) <= 1900 else (text[:1900] + "… (truncated)")
            await channel.send(f"```{payload}```")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ---------- commands ----------

    @commands.command(name="log")
    @commands.has_permissions(manage_guild=True)
    async def set_log_channel(self, ctx: commands.Context, channel_id: Optional[int] = None):
        """
        Set the logging channel with `!log <channel_id>` or by mentioning a channel.
        Use `!log` with no arguments to clear logging for this guild.
        """
        target: Optional[discord.TextChannel] = None

        if ctx.message.channel_mentions:
            target = ctx.me
