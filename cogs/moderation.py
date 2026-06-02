"""
Moderation cog.
/ban, /kick, /mute, /unmute, /warn, /warnings, /clearwarnings, /purge, /slowmode, /userinfo
All moderation actions are logged to the mod log channel.
"""
import os
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db

LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or os.getenv("LOG_CHANNEL_ID", "0"))


async def _mod_log(guild: discord.Guild, embed: discord.Embed):
    if LOG_CHANNEL_ID:
        ch = guild.get_channel(LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass


class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /kick ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You can't kick someone with an equal or higher role.", ephemeral=True)
            return
        try:
            await member.kick(reason=f"{interaction.user} — {reason}")
            embed = _action_embed("👢 Member Kicked", member, interaction.user, reason, discord.Color.orange())
            await interaction.response.send_message(embed=embed)
            await _mod_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to kick that member.", ephemeral=True)

    # ── /ban ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(member="Member to ban", reason="Reason", delete_days="Days of messages to delete (0–7)")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: int = 1):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You can't ban someone with an equal or higher role.", ephemeral=True)
            return
        delete_days = max(0, min(7, delete_days))
        try:
            await member.ban(reason=f"{interaction.user} — {reason}", delete_message_days=delete_days)
            embed = _action_embed("🔨 Member Banned", member, interaction.user, reason, discord.Color.red())
            await interaction.response.send_message(embed=embed)
            await _mod_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to ban that member.", ephemeral=True)

    # ── /unban ────────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Unban a user by their Discord user ID.")
    @app_commands.describe(user_id="Discord user ID to unban", reason="Reason")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await interaction.guild.unban(user, reason=f"{interaction.user} — {reason}")
            embed = discord.Embed(title="✅ User Unbanned", color=discord.Color.green())
            embed.add_field(name="User",        value=f"{user} (ID: {user.id})")
            embed.add_field(name="Unbanned by", value=interaction.user.mention)
            embed.add_field(name="Reason",      value=reason)
            await interaction.response.send_message(embed=embed)
            await _mod_log(interaction.guild, embed)
        except (discord.NotFound, ValueError):
            await interaction.response.send_message("❌ User not found or not banned.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to unban.", ephemeral=True)

    # ── /mute ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="mute", description="Timeout (mute) a member.")
    @app_commands.describe(
        member  = "Member to mute",
        minutes = "Duration in minutes (max 40320 = 28 days)",
        reason  = "Reason",
    )
    @app_commands.default_permissions(moderate_members=True)
    async def mute(self, interaction: discord.Interaction, member: discord.Member, minutes: int = 10, reason: str = "No reason provided"):
        if member.top_role >= interaction.user.top_role:
            await interaction.response.send_message("❌ You can't mute someone with an equal or higher role.", ephemeral=True)
            return
        minutes = max(1, min(40320, minutes))
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user} — {reason}")
            embed = _action_embed(
                f"🔇 Member Muted ({minutes}m)",
                member, interaction.user, reason, discord.Color.dark_orange(),
            )
            await interaction.response.send_message(embed=embed)
            await _mod_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to timeout that member.", ephemeral=True)

    # ── /unmute ───────────────────────────────────────────────────────────────

    @app_commands.command(name="unmute", description="Remove a timeout from a member.")
    @app_commands.describe(member="Member to unmute", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    async def unmute(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        try:
            await member.timeout(None, reason=f"{interaction.user} — {reason}")
            embed = _action_embed("🔊 Member Unmuted", member, interaction.user, reason, discord.Color.green())
            await interaction.response.send_message(embed=embed)
            await _mod_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission.", ephemeral=True)

    # ── /purge ────────────────────────────────────────────────────────────────

    @app_commands.command(name="purge", description="Delete messages in bulk.")
    @app_commands.describe(
        amount = "Number of messages to delete (1–200)",
        member = "Only delete messages from this member (optional)",
    )
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction: discord.Interaction, amount: int, member: discord.Member = None):
        amount = max(1, min(200, amount))
        await interaction.response.defer(ephemeral=True)

        def check(msg):
            return msg.author == member if member else True

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)
            msg = f"🗑️ Deleted **{len(deleted)}** message(s)"
            if member:
                msg += f" from {member.mention}"
            await interaction.followup.send(msg, ephemeral=True)

            embed = discord.Embed(title="🗑️ Messages Purged", color=discord.Color.greyple())
            embed.add_field(name="Count",   value=str(len(deleted)),           inline=True)
            embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
            embed.add_field(name="By",      value=interaction.user.mention,    inline=True)
            if member:
                embed.add_field(name="Target", value=member.mention, inline=True)
            embed.timestamp = discord.utils.utcnow()
            await _mod_log(interaction.guild, embed)
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to delete messages.", ephemeral=True)

    # ── /warn ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Warn a member and log it.")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.default_permissions(manage_messages=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        async with db._lock:
            data     = db._load_raw()
            warnings = data.setdefault("warnings", {})
            uid      = str(member.id)
            warnings.setdefault(uid, []).append({
                "reason":  reason,
                "by":      interaction.user.id,
                "by_name": str(interaction.user),
                "at":      discord.utils.utcnow().isoformat(),
            })
            db._save_raw(data)
            count = len(warnings[uid])

        embed = _action_embed(f"⚠️ Warning #{count}", member, interaction.user, reason, discord.Color.yellow())
        await interaction.response.send_message(embed=embed)
        await _mod_log(interaction.guild, embed)

        try:
            dm_embed = discord.Embed(
                title       = f"⚠️ Warning from {interaction.guild.name}",
                description = f"**Reason:** {reason}\n\nThis is warning **#{count}** on your record.",
                color       = discord.Color.yellow(),
            )
            await member.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    # ── /warnings ─────────────────────────────────────────────────────────────

    @app_commands.command(name="warnings", description="View a member's warning history.")
    @app_commands.describe(member="Member to check")
    @app_commands.default_permissions(manage_messages=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        data          = await db.load()
        user_warnings = data.get("warnings", {}).get(str(member.id), [])

        if not user_warnings:
            await interaction.response.send_message(f"✅ {member.mention} has no warnings.", ephemeral=True)
            return

        lines = [
            f"`{i+1}.` {w['reason']} — by {w['by_name']} on {w['at'][:10]}"
            for i, w in enumerate(user_warnings)
        ]
        embed = discord.Embed(
            title       = f"⚠️ Warnings — {member.display_name}",
            description = "\n".join(lines),
            color       = discord.Color.yellow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"{len(user_warnings)} total warning(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /clearwarnings ────────────────────────────────────────────────────────

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.describe(member="Member to clear")
    @app_commands.default_permissions(manage_guild=True)
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        async with db._lock:
            data = db._load_raw()
            count = len(data.get("warnings", {}).get(str(member.id), []))
            data.setdefault("warnings", {}).pop(str(member.id), None)
            db._save_raw(data)
        await interaction.response.send_message(
            f"✅ Cleared **{count}** warning(s) for {member.mention}.", ephemeral=True
        )

    # ── /slowmode ─────────────────────────────────────────────────────────────

    @app_commands.command(name="slowmode", description="Set slowmode delay on the current channel.")
    @app_commands.describe(seconds="Seconds between messages (0 to disable, max 21600)")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction: discord.Interaction, seconds: int):
        seconds = max(0, min(21600, seconds))
        await interaction.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await interaction.response.send_message("✅ Slowmode disabled.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Slowmode set to **{seconds}s**.", ephemeral=True)

    # ── /userinfo ──────────────────────────────────────────────────────────────

    @app_commands.command(name="userinfo", description="Get info about a server member.")
    @app_commands.describe(member="Member to look up (default: yourself)")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        target     = member or interaction.user
        roblox     = await db.get_roblox_username(target.id)
        ep_rec     = await db.get_ep(roblox) if roblox else None
        data       = await db.load()
        warn_count = len(data.get("warnings", {}).get(str(target.id), []))

        embed = discord.Embed(title=f"👤 {target.display_name}", color=target.color)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="Joined Server",
            value=discord.utils.format_dt(target.joined_at, "R") if target.joined_at else "Unknown",
            inline=True,
        )
        embed.add_field(name="Account Created", value=discord.utils.format_dt(target.created_at, "R"), inline=True)
        embed.add_field(name="Roblox",           value=roblox or "Not verified", inline=True)
        embed.add_field(name="EP",               value=str(ep_rec["ep"]) if ep_rec else "N/A", inline=True)
        embed.add_field(name="Warnings",         value=str(warn_count), inline=True)
        top_role = target.top_role
        embed.add_field(
            name="Top Role",
            value=top_role.mention if top_role != interaction.guild.default_role else "None",
            inline=True,
        )
        await interaction.response.send_message(embed=embed)


def _action_embed(title, member, moderator, reason, color):
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Member",    value=f"{member.mention} ({member})", inline=False)
    embed.add_field(name="Moderator", value=moderator.mention,              inline=True)
    embed.add_field(name="Reason",    value=reason,                         inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    return embed


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))