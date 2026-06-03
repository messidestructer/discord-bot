"""
Role Binds cog.

Automatically assigns Discord roles based on a member's Roblox group rank.
Role binds are stored in the database as: {rank_number: discord_role_id}.

Commands:
  /rolebind add    — bind a Discord role to a Roblox rank
  /rolebind remove — remove a bind
  /rolebind list   — show all current binds
  /rolebind sync   — (owner-only) sync every verified member in the server
  /syncme          — sync your own roles right now

Role syncing also fires automatically on:
  • Verification (complete_verification hook)
  • Member join (if already verified)
  • Rank change (called from roblox_group.py after /rank)
"""
from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.roblox_api import get_group_rank

log = logging.getLogger("role_binds")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))


# ── Database helpers ──────────────────────────────────────────────────────────

async def get_binds() -> dict[int, int]:
    """Return {roblox_rank: discord_role_id}."""
    data = await db.load()
    raw  = data.get("role_binds", {})
    return {int(k): int(v) for k, v in raw.items()}


async def set_bind(rank: int, role_id: int):
    async with db._lock:
        data = db._load_raw()
        data.setdefault("role_binds", {})[str(rank)] = role_id
        db._save_raw(data)


async def remove_bind(rank: int):
    async with db._lock:
        data = db._load_raw()
        data.setdefault("role_binds", {}).pop(str(rank), None)
        db._save_raw(data)


# ── Core sync logic ───────────────────────────────────────────────────────────

async def sync_member_roles(member: discord.Member, roblox_username: str) -> tuple[list[str], list[str]]:
    """
    Fetch the member's current Roblox rank and apply/remove bound roles.
    Returns (added_role_names, removed_role_names).
    """
    from utils.roblox_api import get_user_id_by_name
    binds = await get_binds()
    if not binds:
        return [], []

    roblox_id = await get_user_id_by_name(roblox_username)
    if not roblox_id:
        return [], []

    current_rank = await get_group_rank(roblox_id)  # None if not in group

    guild = member.guild
    added:   list[str] = []
    removed: list[str] = []

    for rank, role_id in binds.items():
        role = guild.get_role(role_id)
        if not role:
            continue
        should_have = (current_rank == rank)
        has_role    = role in member.roles

        if should_have and not has_role:
            try:
                await member.add_roles(role, reason=f"Role bind: rank {rank}")
                added.append(role.name)
            except discord.Forbidden:
                log.warning(f"Missing permission to add role {role.name} to {member}")
        elif not should_have and has_role:
            try:
                await member.remove_roles(role, reason=f"Role bind: rank {rank} no longer held")
                removed.append(role.name)
            except discord.Forbidden:
                log.warning(f"Missing permission to remove role {role.name} from {member}")

    return added, removed


# ── Cog ───────────────────────────────────────────────────────────────────────

class RoleBindsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /rolebind ─────────────────────────────────────────────────────────────

    rolebind = app_commands.Group(
        name="rolebind",
        description="Manage role binds (Discord role ↔ Roblox rank).",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @rolebind.command(name="add", description="Bind a Discord role to a Roblox group rank.")
    @app_commands.describe(
        rank="Roblox group rank number",
        role="Discord role to assign for this rank",
    )
    async def rolebind_add(
        self,
        interaction: discord.Interaction,
        rank: int,
        role: discord.Role,
    ):
        await set_bind(rank, role.id)
        await interaction.response.send_message(
            f"✅ Bound **{role.name}** to Roblox rank **{rank}**.",
            ephemeral=True,
        )

    @rolebind.command(name="remove", description="Remove a role bind for a Roblox rank.")
    @app_commands.describe(rank="Roblox group rank number to unbind")
    async def rolebind_remove(self, interaction: discord.Interaction, rank: int):
        binds = await get_binds()
        if rank not in binds:
            await interaction.response.send_message(
                f"❌ No bind exists for rank **{rank}**.", ephemeral=True
            )
            return
        await remove_bind(rank)
        await interaction.response.send_message(
            f"✅ Removed bind for rank **{rank}**.", ephemeral=True
        )

    @rolebind.command(name="list", description="Show all current role binds.")
    async def rolebind_list(self, interaction: discord.Interaction):
        binds = await get_binds()
        if not binds:
            await interaction.response.send_message(
                "ℹ️ No role binds configured. Use `/rolebind add` to create one.",
                ephemeral=True,
            )
            return

        lines = []
        for rank in sorted(binds):
            role = interaction.guild.get_role(binds[rank])
            role_str = role.mention if role else f"*(deleted role {binds[rank]})*"
            lines.append(f"**Rank {rank}** → {role_str}")

        embed = discord.Embed(
            title="🔗 Role Binds",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(binds)} bind(s) total")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rolebind.command(
        name="sync",
        description="(Owner only) Sync role binds for every verified member in the server.",
    )
    async def rolebind_sync(self, interaction: discord.Interaction):
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Only the bot owner can run a full server sync.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        data    = await db.load()
        verified = data.get("verified_users", {})
        if not verified:
            await interaction.followup.send("ℹ️ No verified members to sync.", ephemeral=True)
            return

        synced  = 0
        skipped = 0
        errors  = 0

        for discord_id_str, roblox_username in verified.items():
            member = interaction.guild.get_member(int(discord_id_str))
            if not member:
                skipped += 1
                continue
            try:
                await sync_member_roles(member, roblox_username)
                synced += 1
            except Exception as e:
                log.warning(f"Error syncing {member}: {e}")
                errors += 1

        msg = f"✅ Sync complete — **{synced}** synced, **{skipped}** not in server"
        if errors:
            msg += f", **{errors}** errors"
        await interaction.followup.send(msg + ".", ephemeral=True)

    # ── /syncme ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="syncme",
        description="Sync your Discord roles to your current Roblox group rank.",
    )
    async def syncme(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        roblox_username = await db.get_roblox_username(interaction.user.id)
        if not roblox_username:
            await interaction.followup.send(
                "❌ You haven't verified your Roblox account yet. Run `/verify` first.",
                ephemeral=True,
            )
            return

        added, removed = await sync_member_roles(interaction.user, roblox_username)

        if not added and not removed:
            await interaction.followup.send(
                "✅ Your roles are already up to date.", ephemeral=True
            )
            return

        parts = []
        if added:
            parts.append(f"Added: {', '.join(added)}")
        if removed:
            parts.append(f"Removed: {', '.join(removed)}")
        await interaction.followup.send(
            "✅ Roles synced!\n" + "\n".join(parts), ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(RoleBindsCog(bot))
