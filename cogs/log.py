# cogs/log.py
# Discord.py cog to log messages, edits, deletions, reactions, and member joins/leaves
# Requirements:
# - discord.py 2.x
# - Intents: members, message_content, reactions, guilds
# Usage:
# 1) Place this file at cogs/log.py
# 2) Load it in your bot's setup_hook(): await bot.load_extension("cogs.log")
# 3) In Discord, run: !log <channel_id> (or mention a channel) to set destination
# Run: !log (no args) to disable logging for this guild.


from __future__ import annotations


import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict


import discord
from discord.ext import commands


try:
# Python 3.9+
from zoneinfo import ZoneInfo # type: ignore
TZ_NY = ZoneInfo("America/New_York")
except Exception:
TZ_NY = None # Fallback to naive if zoneinfo isn't available


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "log_config.json")




@dataclass
class GuildLogConfig:
channel_id: Optional[int] = None


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
self._configs = {
gid: GuildLogConfig.from_dict(cfg) for gid, cfg in data.items()
}
except Exception:
# Corrupt file? Start fresh, but don't crash the bot.
self._configs = {}


def _save(self) -> None:
tmp = {gid: cfg.to_dict() for gid, cfg in self._configs.items()}
os.makedirs(os.path.dirname(self.path), exist_ok=True)
with open(self.path, "w", encoding="utf-8") as f:
json.dump(tmp, f, indent=2)


def get(self, guild_id: int) -> GuildLogConfig:
return self._configs.get(str(guild_id), GuildLogConfig())


def set_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
self._configs[str(guild_id)] = GuildLogConfig(channel_id)
self._save()




class LogCog(commands.Cog, name="Log"):
"""Server logging cog with a simple `!log` command to set destination channel.


Events logged:
- Message create/edit/delete
- Reaction add/remove
- Member join/leave
"""


def __init__(self, bot: commands.Bot | commands.AutoShardedBot):
self.bot = bot
self.store = LoggerStore()


await bot.add_cog(LogCog(bot))
