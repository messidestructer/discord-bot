"""
Profile cog — /profile shows a member's EP, rank, and Roblox info.
"""
import os

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.roblox_api import get_group_rank, get_group_role_name, get_user_id_by_name


class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="profile", description="View your (or another member's) group profile.")
    @app_commands.describe(member="Member to look up (default: yourself)")
    async def profile(self, interaction: discord.Interaction, member: discord.Member = None):
        await interaction.response.defer()
        target = member or interaction.user

        roblox_name = await db.get_roblox_username(target.id)
        ep_rec      = await db.get_ep(roblox_name) if roblox_name else None

        embed = discord.Embed(
            title = f"📋 Profile — {target.display_name}",
            color = target.color or discord.Color.blue(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Discord", value=target.mention, inline=True)

        if roblox_name:
            embed.add_field(name="Roblox", value=roblox_name, inline=True)
            roblox_id = await get_user_id_by_name(roblox_name)
            if roblox_id:
                rank_num  = await get_group_rank(roblox_id)
                rank_name = await get_group_role_name(roblox_id)
                embed.add_field(
                    name="Group Rank",
                    value=(
                        f"{rank_name or 'Not in group'}"
                        + (f" (#{rank_num})" if rank_num else "")
                    ),
                    inline=True,
                )
                embed.add_field(
                    name="Roblox Profile",
                    value=f"[View](https://www.roblox.com/users/{roblox_id}/profile)",
                    inline=True,
                )
        else:
            embed.add_field(name="Roblox", value="Not verified — use `/verify`", inline=True)

        ep = ep_rec["ep"] if ep_rec else 0
        embed.add_field(name="Total EP", value=str(ep), inline=True)

        if ep_rec and ep_rec.get("last_updated"):
            embed.add_field(name="Last Active", value=ep_rec["last_updated"][:10], inline=True)

        # Leaderboard position
        leaderboard = await db.get_leaderboard(9999)
        if roblox_name:
            for i, rec in enumerate(leaderboard):
                if rec.get("username", "").lower() == roblox_name.lower():
                    embed.add_field(name="Leaderboard Rank", value=f"#{i+1}", inline=True)
                    break

        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(ProfileCog(bot))