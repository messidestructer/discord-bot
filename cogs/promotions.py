"""
Auto-promotion cog.
Checks EP records every 6 hours and promotes members in the Roblox group automatically.
Configure PROMOTION_RULES in .env as JSON, or use the defaults below.
"""
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.tasks import loop

import storage.database as db
from utils.roblox_api import (
    get_group_rank,
    get_user_id_by_name,
    set_group_rank_by_number,
)

log = logging.getLogger("promotions")

LOG_CHANNEL_ID       = int(os.getenv("LOG_CHANNEL_ID", "0"))
PROMOTION_CHANNEL_ID = int(os.getenv("PROMOTION_CHANNEL_ID", "0") or os.getenv("LOG_CHANNEL_ID", "0"))

_DEFAULT_RULES = [
    {"min_ep": 0,   "rank": 1,  "name": "Recruit"},
    {"min_ep": 10,  "rank": 5,  "name": "Private"},
    {"min_ep": 25,  "rank": 10, "name": "Corporal"},
    {"min_ep": 50,  "rank": 15, "name": "Sergeant"},
    {"min_ep": 100, "rank": 20, "name": "Lieutenant"},
    {"min_ep": 200, "rank": 25, "name": "Captain"},
    {"min_ep": 350, "rank": 30, "name": "Major"},
    {"min_ep": 500, "rank": 35, "name": "Colonel"},
]


def _load_rules() -> list[dict]:
    raw = os.getenv("PROMOTION_RULES", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            log.warning("Invalid PROMOTION_RULES JSON — using defaults")
    return _DEFAULT_RULES


def _target_rank(ep: int) -> tuple[int, str] | tuple[None, None]:
    rules = sorted(_load_rules(), key=lambda r: r["min_ep"], reverse=True)
    for rule in rules:
        if ep >= rule["min_ep"]:
            return rule["rank"], rule.get("name", str(rule["rank"]))
    return None, None


class PromotionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_promote_task.start()

    def cog_unload(self):
        self.auto_promote_task.cancel()

    @loop(hours=6)
    async def auto_promote_task(self):
        await self._run_promotions(guild=None, announce=True)

    @auto_promote_task.before_loop
    async def before_auto_promote(self):
        await self.bot.wait_until_ready()

    async def _run_promotions(self, guild=None, announce=True) -> list[dict]:
        if not os.getenv("ROBLOX_COOKIE"):
            log.warning("ROBLOX_COOKIE not set — skipping promotions")
            return []

        records  = await db.get_all_ep_records()
        promoted = []

        for key, rec in records.items():
            username    = rec.get("username", key)
            ep          = rec.get("ep", 0)
            target_rank, target_name = _target_rank(ep)
            if target_rank is None:
                continue

            roblox_id = await get_user_id_by_name(username)
            if not roblox_id:
                continue

            current_rank = await get_group_rank(roblox_id)
            if current_rank is None:
                continue  # not in group

            if target_rank <= current_rank:
                continue  # already at or above target

            success = await set_group_rank_by_number(roblox_id, target_rank)
            if success:
                entry = {
                    "username":       username,
                    "old_rank":       current_rank,
                    "new_rank":       target_rank,
                    "new_rank_name":  target_name,
                    "ep":             ep,
                }
                promoted.append(entry)
                await db.log_promotion(username, current_rank, target_rank, ep)
                log.info(f"Promoted {username}: rank {current_rank} → {target_rank} ({ep} EP)")

                if announce and guild and PROMOTION_CHANNEL_ID:
                    ch = guild.get_channel(PROMOTION_CHANNEL_ID)
                    if ch:
                        embed = discord.Embed(
                            title       = "🎉 Auto-Promotion",
                            description = f"**{username}** has been promoted to **{target_name}** (Rank {target_rank})!",
                            color       = discord.Color.gold(),
                        )
                        embed.add_field(name="EP",   value=str(ep),                          inline=True)
                        embed.add_field(name="Rank", value=f"{current_rank} → {target_rank}", inline=True)
                        await ch.send(embed=embed)

        return promoted

    @app_commands.command(name="checkpromotions", description="Manually run the EP promotion check.")
    @app_commands.default_permissions(manage_roles=True)
    async def check_promotions(self, interaction: discord.Interaction):
        await interaction.response.defer()
        promoted = await self._run_promotions(guild=interaction.guild, announce=True)
        if not promoted:
            await interaction.followup.send("✅ No promotions needed — everyone is at the correct rank.")
            return
        lines = [
            f"• **{p['username']}** → {p['new_rank_name']} (Rank {p['new_rank']}) — {p['ep']} EP"
            for p in promoted
        ]
        embed = discord.Embed(
            title       = f"✅ Promoted {len(promoted)} member(s)",
            description = "\n".join(lines),
            color       = discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="promotionrules", description="View the current EP promotion thresholds.")
    async def promotion_rules(self, interaction: discord.Interaction):
        rules = sorted(_load_rules(), key=lambda r: r["min_ep"])
        lines = []
        for r in rules:
            name = r.get("name", f"Rank {r['rank']}")
            lines.append(
                f"**{name}** — {r['min_ep']} EP (Roblox rank #{r['rank']})"
            )
        embed = discord.Embed(
            title       = "📈 Promotion Rules",
            description = "\n".join(lines),
            color       = discord.Color.blue(),
        )
        embed.set_footer(text="Auto-check runs every 6 hours. Use /checkpromotions to run manually.")
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(PromotionsCog(bot))