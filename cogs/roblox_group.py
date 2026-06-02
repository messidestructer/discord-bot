"""
Roblox group management commands.
/rank, /kickroblox, /acceptjoin, /denyjoin, /joinrequests
"""
import os

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.roblox_api import (
    accept_join_request,
    deny_join_request,
    get_group_roles,
    get_join_requests,
    get_user_id_by_name,
    kick_from_group,
    set_group_rank_by_number,
    get_group_rank,
    get_group_role_name,
)

GROUP_ID = os.getenv("ROBLOX_GROUP_ID", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))


def _log_channel(guild: discord.Guild):
    if LOG_CHANNEL_ID:
        return guild.get_channel(LOG_CHANNEL_ID)
    return None


class RobloxGroupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /rank ────────────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="Set a Roblox group member's rank.")
    @app_commands.describe(
        roblox_username="Their Roblox username",
        rank_number="Rank number (1–255). Use /grouproles to see all ranks.",
    )
    @app_commands.default_permissions(manage_roles=True)
    async def rank(self, interaction: discord.Interaction, roblox_username: str, rank_number: int):
        await interaction.response.defer()
        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Roblox user **{roblox_username}** not found.")
            return

        old_rank = await get_group_rank(roblox_id)
        old_role = await get_group_role_name(roblox_id)

        success = await set_group_rank_by_number(roblox_id, rank_number)
        if not success:
            await interaction.followup.send(
                f"❌ Failed to set rank. Make sure rank **{rank_number}** exists and the bot account has permission."
            )
            return

        new_role = await get_group_role_name(roblox_id)
        embed = discord.Embed(
            title="✅ Rank Updated",
            color=discord.Color.green(),
        )
        embed.add_field(name="Player", value=roblox_username, inline=True)
        embed.add_field(name="Old Rank", value=f"{old_role or '?'} (#{old_rank or '?'})", inline=True)
        embed.add_field(name="New Rank", value=f"{new_role or '?'} (#{rank_number})", inline=True)
        embed.add_field(name="Set by", value=interaction.user.mention, inline=True)
        await interaction.followup.send(embed=embed)

        log_ch = _log_channel(interaction.guild)
        if log_ch:
            await log_ch.send(embed=embed)

    # ── /grouproles ──────────────────────────────────────────────────────────

    @app_commands.command(name="grouproles", description="List all roles in the Roblox group.")
    async def grouproles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        roles = await get_group_roles()
        if not roles:
            await interaction.followup.send("❌ Could not fetch group roles.", ephemeral=True)
            return
        lines = [f"**#{r['rank']}** — {r['name']}" for r in sorted(roles, key=lambda x: x["rank"])]
        embed = discord.Embed(
            title=f"Group Roles (Group ID: {GROUP_ID})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /kickroblox ──────────────────────────────────────────────────────────

    @app_commands.command(name="kickroblox", description="Kick a member from the Roblox group.")
    @app_commands.describe(roblox_username="Roblox username to kick from the group")
    @app_commands.default_permissions(manage_guild=True)
    async def kickroblox(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer()
        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Roblox user **{roblox_username}** not found.")
            return

        success = await kick_from_group(roblox_id)
        if success:
            embed = discord.Embed(
                title="🚪 Kicked from Roblox Group",
                description=f"**{roblox_username}** has been removed from the group.",
                color=discord.Color.red(),
            )
            embed.add_field(name="Kicked by", value=interaction.user.mention)
            await interaction.followup.send(embed=embed)
            log_ch = _log_channel(interaction.guild)
            if log_ch:
                await log_ch.send(embed=embed)
        else:
            await interaction.followup.send(
                f"❌ Failed to kick **{roblox_username}**. They may not be in the group, or the bot lacks permissions."
            )

    # ── /joinrequests ─────────────────────────────────────────────────────────

    @app_commands.command(name="joinrequests", description="View pending Roblox group join requests.")
    @app_commands.default_permissions(manage_guild=True)
    async def joinrequests(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        requests = await get_join_requests()
        if not requests:
            await interaction.followup.send("✅ No pending join requests.", ephemeral=True)
            return

        # Paginate into chunks of 10
        chunks = [requests[i:i+10] for i in range(0, len(requests), 10)]
        embeds = []
        for chunk in chunks[:5]:  # max 5 embeds
            lines = []
            for req in chunk:
                uid = req.get("requester", {}).get("userId", "?")
                uname = req.get("requester", {}).get("username", "Unknown")
                lines.append(f"• **{uname}** (ID: {uid})")
            embed = discord.Embed(
                title=f"📋 Join Requests ({len(requests)} pending)",
                description="\n".join(lines),
                color=discord.Color.orange(),
            )
            embeds.append(embed)

        view = JoinRequestsView(requests, interaction.user.id)
        await interaction.followup.send(embeds=embeds[:1], view=view, ephemeral=True)

    # ── /acceptjoin ───────────────────────────────────────────────────────────

    @app_commands.command(name="acceptjoin", description="Accept a Roblox group join request by username.")
    @app_commands.describe(
        roblox_username="Username to accept (or 'all' to accept all pending requests)"
    )
    @app_commands.default_permissions(manage_guild=True)
    async def acceptjoin(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer()
        if roblox_username.lower() == "all":
            requests = await get_join_requests()
            if not requests:
                await interaction.followup.send("No pending join requests.")
                return
            success = 0
            for req in requests:
                uid = req.get("requester", {}).get("userId")
                if uid and await accept_join_request(uid):
                    success += 1
            await interaction.followup.send(f"✅ Accepted **{success}/{len(requests)}** join requests.")
            return

        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Roblox user **{roblox_username}** not found.")
            return

        success = await accept_join_request(roblox_id)
        if success:
            await interaction.followup.send(f"✅ Accepted join request for **{roblox_username}**.")
        else:
            await interaction.followup.send(
                f"❌ Failed to accept. They may not have a pending request or the bot lacks permission."
            )

    # ── /denyjoin ─────────────────────────────────────────────────────────────

    @app_commands.command(name="denyjoin", description="Deny a Roblox group join request by username.")
    @app_commands.describe(roblox_username="Username to deny")
    @app_commands.default_permissions(manage_guild=True)
    async def denyjoin(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer()
        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Roblox user **{roblox_username}** not found.")
            return

        success = await deny_join_request(roblox_id)
        if success:
            await interaction.followup.send(f"✅ Denied join request for **{roblox_username}**.")
        else:
            await interaction.followup.send(f"❌ Failed to deny. They may not have a pending request.")


class JoinRequestsView(discord.ui.View):
    def __init__(self, requests: list, owner_id: int):
        super().__init__(timeout=120)
        self.requests = requests
        self.owner_id = owner_id

    @discord.ui.button(label="Accept All", style=discord.ButtonStyle.green, emoji="✅")
    async def accept_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        success = 0
        for req in self.requests:
            uid = req.get("requester", {}).get("userId")
            if uid and await accept_join_request(uid):
                success += 1
        self.stop()
        await interaction.followup.send(f"✅ Accepted **{success}/{len(self.requests)}** join requests.")

    @discord.ui.button(label="Deny All", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        await interaction.response.defer()
        success = 0
        for req in self.requests:
            uid = req.get("requester", {}).get("userId")
            if uid and await deny_join_request(uid):
                success += 1
        self.stop()
        await interaction.followup.send(f"✅ Denied **{success}/{len(self.requests)}** join requests.")


async def setup(bot):
    await bot.add_cog(RobloxGroupCog(bot))
