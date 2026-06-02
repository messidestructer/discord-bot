"""
EP (Event Points) tracking cog.
/ep manage  — add, subtract, or set EP for a Discord member
/ep check   — check a member's EP
/ep audit   — view recent EP changes for a member
/leaderboard
"""
import os
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db

LOG_CHANNEL_ID     = int(os.getenv("LOG_CHANNEL_ID", "0"))
EP_MANAGER_ROLE_ID = int(os.getenv("EP_MANAGER_ROLE_ID", "0"))

# Rate limit: max 5 EP edits per user per 60 seconds
_rate_buckets: dict[int, list[float]] = defaultdict(list)
RATE_LIMIT  = 5
RATE_WINDOW = 60


def _check_rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    _rate_buckets[user_id] = [t for t in _rate_buckets[user_id] if now - t < RATE_WINDOW]
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

    # ── /ep manage ────────────────────────────────────────────────────────────

    @ep_group.command(name="manage", description="Add, subtract, or set EP for a verified member.")
    @app_commands.describe(
        member    = "The Discord member to modify EP for",
        operation = "What to do with the EP amount",
        amount    = "EP amount (must be positive)",
        note      = "Optional note explaining the change",
    )
    @app_commands.choices(operation=[
        app_commands.Choice(name="Add",      value="add"),
        app_commands.Choice(name="Subtract", value="subtract"),
        app_commands.Choice(name="Set",      value="set"),
    ])
    async def ep_manage(
        self,
        interaction: discord.Interaction,
        member:    discord.Member,
        operation: str,
        amount:    int,
        note:      str = "",
    ):
        if not _has_ep_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("❌ Amount must be a positive number.", ephemeral=True)
            return
        if not _check_rate_limit(interaction.user.id):
            await interaction.response.send_message(
                "⏳ You're editing EP too quickly. Wait a moment.", ephemeral=True
            )
            return

        roblox_username = await db.get_roblox_username(member.id)
        if not roblox_username:
            await interaction.response.send_message(
                f"❌ {member.mention} hasn't verified their Roblox account yet — "
                "they need to run `/verify` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if operation == "add":
            current = await db.get_ep(roblox_username)
            old_ep  = current["ep"] if current else 0
            rec     = await db.set_ep(roblox_username, +amount, interaction.user.id, note)
            title   = "➕ EP Added"
        elif operation == "subtract":
            current = await db.get_ep(roblox_username)
            old_ep  = current["ep"] if current else 0
            rec     = await db.set_ep(roblox_username, -amount, interaction.user.id, note)
            title   = "➖ EP Removed"
        else:  # set
            rec    = await db.set_ep_absolute(roblox_username, amount, interaction.user.id, note)
            old_ep = rec.get("old_ep", 0)
            title  = "✏️ EP Set"

        embed = _ep_embed(title, member, roblox_username, old_ep, rec["ep"], interaction.user, note)
        await interaction.followup.send(embed=embed)
        await _log(interaction.guild, embed)

    # ── /ep check ─────────────────────────────────────────────────────────────

    @ep_group.command(name="check", description="Check a member's current EP total.")
    @app_commands.describe(member="Member to check (leave blank for yourself)")
    async def ep_check(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user

        roblox_username = await db.get_roblox_username(target.id)
        if not roblox_username:
            await interaction.followup.send(
                f"❌ {target.mention} hasn't verified their Roblox account yet. "
                "Use `/verify` to link one.",
                ephemeral=True,
            )
            return

        rec = await db.get_ep(roblox_username)
        ep  = rec["ep"] if rec else 0

        # Leaderboard position
        leaderboard = await db.get_leaderboard(9999)
        position    = next(
            (i + 1 for i, r in enumerate(leaderboard) if r.get("username", "").lower() == roblox_username.lower()),
            None,
        )

        embed = discord.Embed(title=f"📊 EP — {target.display_name}", color=discord.Color.blue())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Discord",    value=target.mention,    inline=True)
        embed.add_field(name="Roblox",     value=roblox_username,   inline=True)
        embed.add_field(name="Total EP",   value=str(ep),           inline=True)
        if position:
            embed.add_field(name="Rank", value=f"#{position}", inline=True)
        if rec and rec.get("last_updated"):
            embed.add_field(name="Last Updated", value=rec["last_updated"][:10], inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /ep audit ─────────────────────────────────────────────────────────────

    @ep_group.command(name="audit", description="View recent EP changes for a member.")
    @app_commands.describe(
        member="Member to audit",
        limit="Number of entries to show (default 10, max 25)",
    )
    @app_commands.default_permissions(manage_roles=True)
    async def ep_audit(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        limit: int = 10,
    ):
        await interaction.response.defer(ephemeral=True)
        limit = min(max(limit, 1), 25)

        roblox_username = await db.get_roblox_username(member.id)
        if not roblox_username:
            await interaction.followup.send(
                f"❌ {member.mention} has no verified Roblox account — no EP history to show.",
                ephemeral=True,
            )
            return

        data  = await db.load()
        audit = [
            e for e in data["ep_audit_log"]
            if e["roblox_username"].lower() == roblox_username.lower()
        ][-limit:][::-1]  # most recent first

        if not audit:
            await interaction.followup.send(
                f"No EP audit entries found for {member.mention} (**{roblox_username}**).",
                ephemeral=True,
            )
            return

        lines = []
        for entry in audit:
            delta = entry["delta"]
            sign  = "+" if delta >= 0 else ""
            note  = f" — *{entry['note']}*" if entry.get("note") else ""
            lines.append(
                f"`{entry['timestamp'][:10]}` {sign}{delta} EP "
                f"({entry['old_ep']} → {entry['new_ep']}){note}"
            )

        embed = discord.Embed(
            title       = f"🔍 EP Audit — {member.display_name}",
            description = "\n".join(lines),
            color       = discord.Color.gold(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Roblox: {roblox_username} | Showing last {len(audit)} entries")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /leaderboard ──────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="View the top EP leaderboard.")
    @app_commands.describe(limit="How many entries to show (default 10, max 25)")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()
        limit   = min(max(limit, 1), 25)
        records = await db.get_leaderboard(limit)

        if not records:
            await interaction.followup.send("No EP records yet.")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, rec in enumerate(records):
            medal   = medals[i] if i < 3 else f"`#{i+1}`"
            mention = ""
            if rec.get("discord_id"):
                m = interaction.guild.get_member(int(rec["discord_id"]))
                mention = f" ({m.mention})" if m else ""
            lines.append(f"{medal} **{rec['username']}**{mention} — {rec['ep']} EP")

        embed = discord.Embed(
            title       = "🏆 EP Leaderboard",
            description = "\n".join(lines),
            color       = discord.Color.gold(),
        )
        embed.set_footer(text=f"Showing top {len(records)} members")
        await interaction.followup.send(embed=embed)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ep_embed(
    title:           str,
    member:          discord.Member,
    roblox_username: str,
    old_ep:          int,
    new_ep:          int,
    editor:          discord.Member,
    note:            str,
) -> discord.Embed:
    delta = new_ep - old_ep
    color = discord.Color.green() if delta >= 0 else discord.Color.red()
    embed = discord.Embed(title=title, color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member",    value=member.mention,                        inline=True)
    embed.add_field(name="Roblox",    value=roblox_username,                       inline=True)
    embed.add_field(name="Change",    value=f"{'+' if delta >= 0 else ''}{delta}", inline=True)
    embed.add_field(name="New Total", value=str(new_ep),                           inline=True)
    embed.add_field(name="Edited by", value=editor.mention,                        inline=True)
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