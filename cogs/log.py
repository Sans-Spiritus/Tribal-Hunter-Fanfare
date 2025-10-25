# cogs/log.py
# Discord.py cog to log messages, edits, deletions, reactions, and member joins/leaves
# Requirements:
#   - discord.py 2.x
#   - Intents: members, message_content, reactions, guilds
# Usage:
#   1) Place this file at cogs/log.py
#   2) Load it in your bot's setup_hook():  await bot.load_extension("cogs.log")
#   3) In Discord, run:  !log <channel_id>   (or mention a channel) to set destination
#      Run:  !log      (no args) to disable logging for this guild.

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict

import discord
from discord.ext import commands

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "log_config.json")
 = os.path.join(os.path.dirname(__file__), "log_config.json")


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

    # ----------------------- Utilities -----------------------

    @staticmethod
    def _now_text() -> str:
        # Resolve Eastern time dynamically; if zoneinfo isn't available, fall back to UTC.
        try:
            from zoneinfo import ZoneInfo  # type: ignore
            dt = datetime.now(tz=ZoneInfo("America/New_York"))
        except Exception:
            dt = datetime.utcnow()
        return dt.strftime("%H:%M:%S")  # 24h HH:MM:SS

    @staticmethod
    def _safe_text(s: Optional[str], limit: int = 1200) -> str:
        if not s:
            return ""
        # Collapse newlines for compact log lines and avoid super long spam
        s_clean = s.replace("\n", "\\n")
        if len(s_clean) > limit:
            s_clean = s_clean[:limit] + "… (truncated)"
        return s_clean

    @staticmethod
    def _emoji_name(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
        if isinstance(emoji, str):
            return emoji
        if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
            return emoji.name or str(emoji)
        return str(emoji)

    def _log_channel(self, guild: Optional[discord.Guild]) -> Optional[discord.TextChannel]:
        if not guild:
            return None
        cfg = self.store.get(guild.id)
        if not cfg.channel_id:
            return None
        ch = guild.get_channel(cfg.channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        return None

    async def _send_log(self, guild: Optional[discord.Guild], text: str) -> None:
        if not guild:
            return
        channel = self._log_channel(guild)
        if not channel:
            return
        try:
            if len(text) <= 1900:
                await channel.send(f"```{text}```")
            else:
                await channel.send(f"```{text[:1900]}… (truncated)```")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    # ----------------------- Commands -----------------------

    @commands.command(name="log")
    @commands.has_permissions(manage_guild=True)
    async def set_log_channel(self, ctx: commands.Context, channel_id: Optional[int] = None):
        """
        Set the logging channel with `!log <channel_id>` or by mentioning a channel.
        Use `!log` with no arguments to clear logging for this guild.
        """
        target: Optional[discord.TextChannel] = None

        if ctx.message.channel_mentions:
            target = ctx.message.channel_mentions[0]  # first mentioned channel wins
        elif channel_id is not None:
            ch = ctx.guild.get_channel(int(channel_id))
            if isinstance(ch, discord.TextChannel):
                target = ch

        if target is None and channel_id is None and not ctx.message.channel_mentions:
            # Clear
            self.store.set_channel(ctx.guild.id, None)
            return await ctx.reply("✅ Logging disabled for this server.", mention_author=False)

        if target is None:
            return await ctx.reply("❌ Please provide a valid text channel ID or mention a channel.", mention_author=False)

        self.store.set_channel(ctx.guild.id, target.id)
        await ctx.reply(f"✅ Logging channel set to {target.mention} (ID: `{target.id}`).", mention_author=False)

    @commands.command(name="logtest")
    @commands.has_permissions(manage_guild=True)
    async def log_test(self, ctx: commands.Context):
        await self._send_log(ctx.guild, f"[{self._now_text()}] Test | Logger online in #{ctx.channel.name}")
        await ctx.reply("Sent a test line to the configured logging channel.", mention_author=False)

    # ----------------------- Format helpers -----------------------

    @staticmethod
    def _user_tag(user: discord.abc.User | discord.Object):
        name = getattr(user, "name", None) or getattr(user, "display_name", None) or "User"
        uid = getattr(user, "id", 0)
        return f"User {name} {uid}"

    @staticmethod
    def _chan_label(channel: discord.abc.GuildChannel | discord.Thread | None) -> str:
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return f"#{channel.name}"
        return "[unknown-channel]"

    # ----------------------- Events -----------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        ts = self._now_text()
        content = self._safe_text(message.content)
        line = (
            f"[{ts}] {self._user_tag(message.author)} | Sent Message {message.id} {content} | "
            f"in {self._chan_label(message.channel)}"
        )
        await self._send_log(message.guild, line)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot:
            return
        ts = self._now_text()
        content = self._safe_text(after.content)
        line = f"[{ts}] {self._user_tag(after.author)} | Edited Message {after.id} {content}"
        await self._send_log(after.guild, line)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        # Only fires when the message is cached; content available
        if not message.guild:
            return
        author = message.author or discord.Object(id=0)
        ts = self._now_text()
        content = self._safe_text(getattr(message, "content", "")) or "[no cached content]"
        line = (
            f"[{ts}] {self._user_tag(author)} | Removed Message {content} | "
            f"in {self._chan_label(message.channel)}"
        )
        await self._send_log(message.guild, line)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # Fallback when not cached; log minimal info
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if not guild:
            return
        ts = self._now_text()
        ch = guild.get_channel(payload.channel_id)
        line = (
            f"[{ts}] User [unknown] 0 | Removed Message [unknown content] | in {self._chan_label(ch)}"
        )
        await self._send_log(guild, line)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        user = guild.get_member(payload.user_id) or discord.Object(id=payload.user_id)
        channel = guild.get_channel(payload.channel_id)
        content = ""
        try:
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(payload.message_id)
                content = self._safe_text(msg.content)
        except Exception:
            content = ""
        ts = self._now_text()
        emoji_name = self._emoji_name(payload.emoji)
        line = (
            f"[{ts}] {self._user_tag(user)} | Added Reaction {emoji_name} to {payload.message_id} {content} | "
            f"in {self._chan_label(channel)}"
        )
        await self._send_log(guild, line)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        user = guild.get_member(payload.user_id) or discord.Object(id=payload.user_id)
        channel = guild.get_channel(payload.channel_id)
        content = ""
        try:
            if isinstance(channel, discord.TextChannel):
                msg = await channel.fetch_message(payload.message_id)
                content = self._safe_text(msg.content)
        except Exception:
            content = ""
        ts = self._now_text()
        emoji_name = self._emoji_name(payload.emoji)
        line = (
            f"[{ts}] {self._user_tag(user)} | Removed Reaction {emoji_name} from {payload.message_id} {content} | "
            f"in {self._chan_label(channel)}"
        )
        await self._send_log(guild, line)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        ts = self._now_text()
        line = f"[{ts}] {self._user_tag(member)} | Joined the server"
        await self._send_log(member.guild, line)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        ts = self._now_text()
        line = f"[{ts}] {self._user_tag(member)} | Left the server"
        await self._send_log(member.guild, line)


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))
