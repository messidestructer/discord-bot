"""
Fun and utility commands.
/ping, /botinfo, /serverinfo, /avatar, /rolemembers, /poll
"""
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db


class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._start_time = time.monotonic()

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! `{latency}ms`")

    @app_commands.command(name="botinfo", description="Show info about this bot.")
    async def botinfo(self, interaction: discord.Interaction):
        uptime_s = int(time.monotonic() - self._start_time)
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        records = await db.get_all_ep_records()
        embed = discord.Embed(title="🤖 Bot Info", color=discord.Color.blue())
        embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency*1000)}ms", inline=True)
        embed.add_field(name="EP Records", value=str(len(records)), inline=True)
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Show info about this server.")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        embed = discord.Embed(title=g.name, color=discord.Color.blurple())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Members", value=str(g.member_count), inline=True)
        embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        embed.add_field(name="Channels", value=str(len(g.channels)), inline=True)
        embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D"), inline=True)
        embed.set_footer(text=f"Server ID: {g.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Get someone's avatar.")
    @app_commands.describe(member="Member (default: yourself)")
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        embed = discord.Embed(title=f"{target.display_name}'s Avatar", color=discord.Color.blue())
        embed.set_image(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rolemembers", description="List members with a specific role.")
    @app_commands.describe(role="The role to list members of")
    async def rolemembers(self, interaction: discord.Interaction, role: discord.Role):
        members = role.members
        if not members:
            await interaction.response.send_message(f"No members have the {role.mention} role.", ephemeral=True)
            return
        chunks = [members[i:i+30] for i in range(0, len(members), 30)]
        lines = ", ".join(m.display_name for m in chunks[0])
        extra = len(members) - len(chunks[0])
        desc = lines
        if extra > 0:
            desc += f" *...and {extra} more*"
        embed = discord.Embed(
            title=f"👥 {role.name} — {len(members)} members",
            description=desc,
            color=role.color,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="poll", description="Create a yes/no or custom poll.")
    @app_commands.describe(
        question="The poll question",
        option_a="First option (default: Yes)",
        option_b="Second option (default: No)",
    )
    async def poll(self, interaction: discord.Interaction, question: str,
                   option_a: str = "Yes", option_b: str = "No"):
        embed = discord.Embed(
            title=f"📊 {question}",
            description=f"🇦 {option_a}\n🇧 {option_b}",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Poll by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await msg.add_reaction("🇦")
        await msg.add_reaction("🇧")

    @app_commands.command(name="setnick", description="Set your own nickname to your Roblox username.")
    async def setnick(self, interaction: discord.Interaction):
        roblox = await db.get_roblox_username(interaction.user.id)
        if not roblox:
            await interaction.response.send_message(
                "❌ You haven't verified yet. Use `/verify` first.", ephemeral=True
            )
            return
        try:
            await interaction.user.edit(nick=roblox)
            await interaction.response.send_message(f"✅ Nickname set to **{roblox}**.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't change your nickname (you may be the server owner).", ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(FunCog(bot))
