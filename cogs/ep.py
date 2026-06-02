"""
EP (Event Points) tracking cog.
/ep add, /ep subtract, /ep set, /ep check, /ep audit, /leaderboard
"""
import os
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
EP_MANAGER_ROLE_ID = int(os.getenv("EP_MANAGER_ROLE_ID", "0"))

# Simple rate limit: max 5 EP edits per user per 60 seconds
_rate_buckets: dict[int, list[float]] = defaultdict(list)
RATE_LIMIT = 5
RATE_WINDOW = 60


def _check_rate_limit(user_id: int) -> bool:
    """Returns True if allowed, False if rate-limited."""
    now = time.monotonic()
    bucket = _rate_buckets[user_id]
    _rate_buckets[user_id] = [t for t in bucket if now - t < RATE_WINDOW]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT:
        return False
    _rate_buckets[user_id].append(now)
    return True


def _has_ep_permission(interaction: discord.Interaction) -> bool:
    if EP_MANAGER_ROLE_ID and interaction.guild:
        role = interaction.guild.get_role(EP_MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            return True
    return interaction.user.guild_permissions.manage_roles


class EPCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    ep_group = app_commands.Group(name="ep", description="Event Points commands")

    @ep_group.command(name="add", description="Add EP to a Roblox member.")
    @app_commands.describe(roblox_username="Roblox username", amount="EP to add", note="Optional note")
    async def ep_add(self, interaction: discord.Interaction, roblox_username: str, amount: int, note: str = ""):
        if not _has_ep_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be positive. Use `/ep subtract` to remove EP.", ephemeral=True)
            return
        if not _check_rate_limit(interaction.user.id):
            await interaction.response.send_message("⏳ You're editing EP too fast. Wait a moment.", ephemeral=True)
            return

        await interaction.response.defer()
        rec = await db.set_ep(roblox_username, amount, interaction.user.id, note)
        embed = _ep_embed("➕ EP Added", roblox_username, rec["ep"] - amount, rec["ep"], interaction.user, note)
        await interaction.followup.send(embed=embed)
        await _log(interaction.guild, embed)

    @ep_group.command(name="subtract", description="Remove EP from a Roblox member.")
    @app_commands.describe(roblox_username="Roblox username", amount="EP to remove", note="Optional note")
    async def ep_subtract(self, interaction: discord.Interaction, roblox_username: str, amount: int, note: str = ""):
        if not _has_ep_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
            return
        if not _check_rate_limit(interaction.user.id):
            await interaction.response.send_message("⏳ Rate limited. Wait a moment.", ephemeral=True)
            return

        await interaction.response.defer()
        rec = await db.set_ep(roblox_username, -amount, interaction.user.id, note)
        embed = _ep_embed("➖ EP Removed", roblox_username, rec["ep"] + amount, rec["ep"], interaction.user, note)
        await interaction.followup.send(embed=embed)
        await _log(interaction.guild, embed)

    @ep_group.command(name="check", description="Check a member's EP.")
    @app_commands.describe(roblox_username="Roblox username (leave blank for yourself)")
    async def ep_check(self, interaction: discord.Interaction, roblox_username: str = None):
        await interaction.response.defer(ephemeral=True)
        if not roblox_username:
            roblox_username = await db.get_roblox_username(interaction.user.id)
            if not roblox_username:
                await interaction.followup.send("❌ You haven't verified your Roblox account. Use `/verify`.", ephemeral=True)
                return

        rec = await db.get_ep(roblox_username)
        if not rec:
            await interaction.followup.send(f"No EP record found for **{roblox_username}**.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📊 EP — {rec['username']}", color=discord.Color.blue())
        embed.add_field(name="Total EP", value=str(rec["ep"]), inline=True)
        embed.add_field(name="Last Updated", value=rec.get("last_updated", "Unknown")[:10], inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ep_group.command(name="audit", description="View recent EP changes for a member.")
    @app_commands.describe(roblox_username="Roblox username")
    @app_commands.default_permissions(manage_roles=True)
    async def ep_audit(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer(ephemeral=True)
        data = await db.load()
        audit = [
            e for e in data["ep_audit_log"]
            if e["roblox_username"].lower() == roblox_username.lower()
        ][-10:][::-1]

        if not audit:
            await interaction.followup.send(f"No audit entries for **{roblox_username}**.", ephemeral=True)
            return

        lines = []
        for entry in audit:
            delta = entry["delta"]
            sign = "+" if delta >= 0 else ""
            lines.append(
                f"`{entry['timestamp'][:10]}` {sign}{delta} EP "
                f"({entry['old_ep']} → {entry['new_ep']})"
                + (f" — *{entry['note']}*" if entry.get("note") else "")
            )
        embed = discord.Embed(
            title=f"🔍 EP Audit — {roblox_username}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="leaderboard", description="View the top EP leaderboard.")
    @app_commands.describe(limit="How many entries to show (default 10, max 25)")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()
        limit = min(max(limit, 1), 25)
        records = await db.get_leaderboard(limit)
        if not records:
            await interaction.followup.send("No EP records yet.")
            return
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, rec in enumerate(records):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            lines.append(f"{medal} **{rec['username']}** — {rec['ep']} EP")
        embed = discord.Embed(
            title="🏆 EP Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed)


def _ep_embed(title: str, username: str, old_ep: int, new_ep: int,
              editor: discord.Member, note: str) -> discord.Embed:
    delta = new_ep - old_ep
    color = discord.Color.green() if delta >= 0 else discord.Color.red()
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Player", value=username, inline=True)
    embed.add_field(name="Change", value=f"{'+' if delta >= 0 else ''}{delta}", inline=True)
    embed.add_field(name="New Total", value=str(new_ep), inline=True)
    embed.add_field(name="Edited by", value=editor.mention, inline=True)
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    return embed


async def _log(guild: discord.Guild, embed: discord.Embed):
    if LOG_CHANNEL_ID and guild:
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass


async def setup(bot):
    await bot.add_cog(EPCog(bot))
