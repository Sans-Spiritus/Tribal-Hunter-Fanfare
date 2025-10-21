from __future__ import annotations
from cogs.utils.economy_adapter import EconomyAdapter
cfg.cooldown = max(1, seconds)
self.store.set_act(cfg)
await ctx.reply(f"Updated cooldown for {act} → {cfg.cooldown}s")


@hobby_group.command(name="set-reward")
@commands.has_permissions(manage_guild=True)
async def hobby_set_reward(self, ctx: commands.Context, act: str, min_coins: int, max_coins: int):
cfg = self.store.get_act(act)
if not cfg:
return await ctx.reply("Act not found.")
cfg.reward_min = min( min_coins, max_coins )
cfg.reward_max = max( min_coins, max_coins )
cfg.use_drops = False
self.store.set_act(cfg)
await ctx.reply(f"{act} now uses range rewards: {cfg.reward_min}-{cfg.reward_max} coins")


@hobby_group.command(name="use-drops")
@commands.has_permissions(manage_guild=True)
async def hobby_use_drops(self, ctx: commands.Context, act: str, enabled: str):
cfg = self.store.get_act(act)
if not cfg:
return await ctx.reply("Act not found.")
cfg.use_drops = enabled.lower() in ("true", "1", "yes", "y", "on")
self.store.set_act(cfg)
await ctx.reply(f"{act} drops mode → {cfg.use_drops}")


@hobby_group.command(name="add-drop")
@commands.has_permissions(manage_guild=True)
async def hobby_add_drop(self, ctx: commands.Context, act: str, label: str, value: int, weight: int):
cfg = self.store.get_act(act)
if not cfg:
return await ctx.reply("Act not found.")
cfg.drops.append(Drop(label=label, value=max(0, value), weight=max(1, weight)))
self.store.set_act(cfg)
await ctx.reply(f"Added drop to {act}: {label} (value {value}, weight {weight})")


@hobby_group.command(name="clear-drops")
@commands.has_permissions(manage_guild=True)
async def hobby_clear_drops(self, ctx: commands.Context, act: str):
cfg = self.store.get_act(act)
if not cfg:
return await ctx.reply("Act not found.")
cfg.drops.clear()
self.store.set_act(cfg)
await ctx.reply(f"Cleared drops for {act}.")


@hobby_group.command(name="add-dialog")
@commands.has_permissions(manage_guild=True)
async def hobby_add_dialog(self, ctx: commands.Context, act: str, *, text: str):
cfg = self.store.get_act(act)
if not cfg:
return await ctx.reply("Act not found.")
text = text.strip()
if not text:
return await ctx.reply("Dialog text cannot be empty.")
cfg.dialogs.append(text)
self.store.set_act(cfg)
await ctx.reply(f"Added dialog to {act}: {text}")


@hobby_group.command(name="new-act")
@commands.has_permissions(manage_guild=True)
async def hobby_new_act(self, ctx: commands.Context, name: str):
name = name.lower()
if self.store.get_act(name):
return await ctx.reply("That act already exists.")
cfg = ActConfig(name=name, cooldown=60, reward_min=10, reward_max=20, use_drops=False, dialogs=[
"行動完了！",
"いい感じにこなせた。",
"少しだけ疲れたけど成果あり。",
"予定通りに進んだ。",
"運も味方したみたい。",
])
self.store.set_act(cfg)
await ctx.reply(f"Created new act: {name}")


# ---- setup ----
async def setup(bot: commands.Bot):
await bot.add_cog(Hobby(bot))
