"""
Weekly report cog.
/genreport — generates a text-based summary embed (no paid AI needed).
/weeklystats — shows this week's stats in Discord.
Also posts weekly Google Sheets sync.
"""
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.tasks import loop

import storage.database as db
from utils.sheets import generate_weekly_report_data, sync_ep_leaderboard, sync_event_log

REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", "0") or os.getenv("LOG_CHANNEL_ID", "0"))
UNIT_NAME = os.getenv("REPORT_UNIT_NAME", "The Group")


class ReportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.weekly_auto_report.start()
        self.sheets_sync.start()

    def cog_unload(self):
        self.weekly_auto_report.cancel()
        self.sheets_sync.cancel()

    @loop(hours=24)
    async def weekly_auto_report(self):
        """Auto-post weekly report every Sunday."""
        now = datetime.now(timezone.utc)
        if now.weekday() == 6:  # Sunday
            guild_id = int(os.getenv("GUILD_ID", "0"))
            if not guild_id:
                return
            guild = self.bot.get_guild(guild_id)
            if guild and REPORT_CHANNEL_ID:
                ch = guild.get_channel(REPORT_CHANNEL_ID)
                if ch:
                    embeds = await _build_report_embeds()
                    await ch.send(content="📊 **Weekly Activity Report**", embeds=embeds)

    @weekly_auto_report.before_loop
    async def before_weekly(self):
        await self.bot.wait_until_ready()

    @loop(hours=int(os.getenv("EP_SYNC_INTERVAL_HOURS", "6")))
    async def sheets_sync(self):
        """Sync EP and events to Google Sheets on schedule."""
        records = await db.get_all_ep_records()
        events = await db.get_recent_events(500)
        await sync_ep_leaderboard(records)
        await sync_event_log(events)

    @sheets_sync.before_loop
    async def before_sheets(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="genreport", description="Generate and post the weekly activity report.")
    @app_commands.default_permissions(manage_guild=True)
    async def gen_report(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embeds = await _build_report_embeds()
        await interaction.followup.send(content="📊 **Weekly Activity Report**", embeds=embeds)

    @app_commands.command(name="weeklystats", description="Show this week's event stats.")
    async def weekly_stats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        events = await db.get_events_this_week()
        records = await db.get_all_ep_records()
        stats = await generate_weekly_report_data(events, records)

        embed = discord.Embed(
            title=f"📅 This Week — {UNIT_NAME}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Events Logged", value=str(stats["total_events"]), inline=True)
        embed.add_field(name="Total EP Awarded", value=str(stats["total_ep_awarded"]), inline=True)
        embed.add_field(name="Unique Participants", value=str(stats["unique_participants"]), inline=True)
        embed.add_field(name="⭐ Most Active Player", value=f"{stats['most_active_player']} ({stats['most_active_ep']} EP)", inline=True)
        embed.add_field(name="🎯 Most Events Hosted", value=f"{stats['most_events_hosted']} ({stats['most_events_hosted_count']} events)", inline=True)

        by_type = stats.get("events_by_type", {})
        if by_type:
            type_str = "\n".join(f"• {k}: {v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1]))
            embed.add_field(name="Events by Type", value=type_str, inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="syncsheets", description="Manually sync EP and events to Google Sheets.")
    @app_commands.default_permissions(manage_guild=True)
    async def sync_sheets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        records = await db.get_all_ep_records()
        events = await db.get_recent_events(500)
        await sync_ep_leaderboard(records)
        await sync_event_log(events)
        await interaction.followup.send("✅ Synced to Google Sheets.", ephemeral=True)


async def _build_report_embeds() -> list[discord.Embed]:
    events = await db.get_events_this_week()
    records = await db.get_all_ep_records()
    stats = await generate_weekly_report_data(events, records)

    embeds = []

    # Summary embed
    summary = discord.Embed(
        title=f"📊 Weekly Report — {UNIT_NAME}",
        description=f"Week ending <t:{int(datetime.now(timezone.utc).timestamp())}:D>",
        color=discord.Color.gold(),
    )
    summary.add_field(name="Total Events", value=str(stats["total_events"]), inline=True)
    summary.add_field(name="Total EP Awarded", value=str(stats["total_ep_awarded"]), inline=True)
    summary.add_field(name="Unique Participants", value=str(stats["unique_participants"]), inline=True)
    summary.add_field(name="⭐ Most Active Player", value=f"**{stats['most_active_player']}** — {stats['most_active_ep']} EP this week", inline=False)
    summary.add_field(name="🎯 Most Events Hosted", value=f"**{stats['most_events_hosted']}** — {stats['most_events_hosted_count']} events", inline=False)
    embeds.append(summary)

    # Top 10 leaderboard embed
    if stats["top_10_leaderboard"]:
        lb = discord.Embed(title="🏆 Top 10 EP Leaderboard (All Time)", color=discord.Color.gold())
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, rec in enumerate(stats["top_10_leaderboard"]):
            medal = medals[i] if i < 3 else f"`#{i+1}`"
            lines.append(f"{medal} **{rec['username']}** — {rec['ep']} EP")
        lb.description = "\n".join(lines)
        embeds.append(lb)

    # Weekly top gainers
    gainers = stats.get("weekly_ep_gainers", [])
    if gainers:
        ge = discord.Embed(title="📈 Top EP Earners This Week", color=discord.Color.green())
        lines = [f"`#{i+1}` **{name}** — +{ep} EP" for i, (name, ep) in enumerate(gainers)]
        ge.description = "\n".join(lines)
        embeds.append(ge)

    return embeds[:10]  # Discord max 10 embeds per message


async def setup(bot):
    await bot.add_cog(ReportCog(bot))
