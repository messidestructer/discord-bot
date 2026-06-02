"""
Event logging cog.
/log — logs an event, awards EP to all attendees.
Supports OCR (pytesseract, free) or manual name entry.
"""
import io
import json
import os
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.ocr import extract_usernames_from_image, parse_manual_names

LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
EP_MANAGER_ROLE_ID = int(os.getenv("EP_MANAGER_ROLE_ID", "0"))
LOG_MODE = os.getenv("LOG_MODE", "flexible")  # flexible | screenshot_required | manual_only

EVENTS_CONFIG_PATH = os.getenv("EVENTS_CONFIG_PATH", "events_config.json")
_DEFAULT_EVENTS = [
    {"name": "Training", "ep": 2},
    {"name": "Patrol", "ep": 1},
    {"name": "Tryout", "ep": 3},
    {"name": "Joint Op", "ep": 4},
    {"name": "Meeting", "ep": 1},
]

# Rate limit: max 3 log uses per 5 minutes
_rate_buckets: dict[int, list[float]] = defaultdict(list)


def _load_events() -> list[dict]:
    if os.path.exists(EVENTS_CONFIG_PATH):
        with open(EVENTS_CONFIG_PATH) as f:
            return json.load(f)
    return _DEFAULT_EVENTS


def _check_rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    bucket = [t for t in _rate_buckets[user_id] if now - t < 300]
    _rate_buckets[user_id] = bucket
    if len(bucket) >= 3:
        return False
    _rate_buckets[user_id].append(now)
    return True


def _has_log_permission(interaction: discord.Interaction) -> bool:
    if EP_MANAGER_ROLE_ID and interaction.guild:
        role = interaction.guild.get_role(EP_MANAGER_ROLE_ID)
        if role and role in interaction.user.roles:
            return True
    return interaction.user.guild_permissions.manage_roles


def _event_choices() -> list[app_commands.Choice]:
    events = _load_events()
    return [app_commands.Choice(name=f"{e['name']} ({e['ep']} EP)", value=e["name"]) for e in events[:25]]


class LogEventCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="log", description="Log an event and award EP to attendees.")
    @app_commands.describe(
        event_type="Type of event",
        screenshot="Screenshot of the event (optional in flexible mode)",
        note="Optional note about the event",
    )
    @app_commands.choices(event_type=_event_choices())
    async def log_event(
        self,
        interaction: discord.Interaction,
        event_type: str,
        screenshot: discord.Attachment = None,
        note: str = "",
    ):
        if not _has_log_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return
        if not _check_rate_limit(interaction.user.id):
            await interaction.response.send_message("⏳ Rate limited. Max 3 event logs per 5 minutes.", ephemeral=True)
            return

        events = _load_events()
        event_cfg = next((e for e in events if e["name"] == event_type), None)
        if not event_cfg:
            await interaction.response.send_message(f"❌ Unknown event type: {event_type}", ephemeral=True)
            return

        ep_per_player = event_cfg["ep"]

        # Decide flow based on mode and screenshot presence
        if LOG_MODE == "manual_only" or (LOG_MODE == "flexible" and not screenshot):
            # Show manual entry modal
            modal = ManualEntryModal(event_type, ep_per_player, note, interaction.user, interaction.guild)
            await interaction.response.send_modal(modal)
            return

        if LOG_MODE == "screenshot_required" and not screenshot:
            await interaction.response.send_message("❌ A screenshot is required to log events.", ephemeral=True)
            return

        # OCR path
        await interaction.response.defer(ephemeral=True)
        image_bytes = await screenshot.read()
        usernames = extract_usernames_from_image(image_bytes)

        if not usernames:
            # OCR found nothing — fall back to manual
            modal = ManualEntryModal(event_type, ep_per_player, note, interaction.user, interaction.guild)
            await interaction.response.send_modal(modal)
            return

        view = ConfirmLogView(event_type, ep_per_player, usernames, note, interaction.user, interaction.guild)
        embed = _preview_embed(event_type, ep_per_player, usernames, source="OCR")
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class ManualEntryModal(discord.ui.Modal, title="Log Event — Enter Names"):
    names = discord.ui.TextInput(
        label="Roblox Usernames (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Username1\nUsername2\nUsername3",
        required=True,
        max_length=2000,
    )

    def __init__(self, event_type, ep_per_player, note, host, guild):
        super().__init__()
        self.event_type = event_type
        self.ep_per_player = ep_per_player
        self.note = note
        self.host = host
        self.guild = guild

    async def on_submit(self, interaction: discord.Interaction):
        usernames = parse_manual_names(self.names.value)
        if not usernames:
            await interaction.response.send_message("❌ No valid usernames found.", ephemeral=True)
            return
        view = ConfirmLogView(self.event_type, self.ep_per_player, usernames, self.note, self.host, self.guild)
        embed = _preview_embed(self.event_type, self.ep_per_player, usernames, source="manual")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class EditNamesModal(discord.ui.Modal, title="Edit Attendee List"):
    names = discord.ui.TextInput(
        label="Roblox Usernames (one per line)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, original_names: list, confirm_view):
        super().__init__()
        self.confirm_view = confirm_view
        self.names.default = "\n".join(original_names)

    async def on_submit(self, interaction: discord.Interaction):
        new_names = parse_manual_names(self.names.value)
        self.confirm_view.usernames = new_names
        embed = _preview_embed(
            self.confirm_view.event_type,
            self.confirm_view.ep_per_player,
            new_names,
            source="edited",
        )
        await interaction.response.edit_message(embed=embed, view=self.confirm_view)


class ConfirmLogView(discord.ui.View):
    def __init__(self, event_type, ep_per_player, usernames, note, host, guild):
        super().__init__(timeout=300)
        self.event_type = event_type
        self.ep_per_player = ep_per_player
        self.usernames = usernames
        self.note = note
        self.host = host
        self.guild = guild

    @discord.ui.button(label="✅ Confirm & Award EP", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command user can confirm.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        self.stop()

        # Award EP to each attendee
        for username in self.usernames:
            await db.set_ep(username, self.ep_per_player, self.host.id, f"Event: {self.event_type}")

        # Log the event
        entry = await db.log_event(
            event_type=self.event_type,
            ep_awarded=self.ep_per_player,
            participants=self.usernames,
            host_discord_id=self.host.id,
            host_name=str(self.host),
            note=self.note,
        )

        embed = _result_embed(entry, self.host)

        # Post to log channel
        log_channel_id = int(os.getenv("LOG_CHANNEL_ID", "0"))
        event_log_channel_id = int(os.getenv("EVENT_LOG_CHANNEL_ID", "0") or log_channel_id)
        if event_log_channel_id and self.guild:
            ch = self.guild.get_channel(event_log_channel_id)
            if ch:
                await ch.send(embed=embed)

        await interaction.followup.send(
            f"✅ Logged **{self.event_type}** — {len(self.usernames)} attendees received **{self.ep_per_player} EP** each.",
            ephemeral=True,
        )

    @discord.ui.button(label="✏️ Edit Names", style=discord.ButtonStyle.secondary)
    async def edit_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command user can edit.", ephemeral=True)
            return
        modal = EditNamesModal(self.usernames, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command user can cancel.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Event log cancelled.", embed=None, view=None)


def _preview_embed(event_type: str, ep: int, usernames: list, source: str) -> discord.Embed:
    display = usernames[:30]
    extra = len(usernames) - 30
    lines = "\n".join(display)
    if extra > 0:
        lines += f"\n*...and {extra} more*"
    embed = discord.Embed(
        title=f"📋 Preview — {event_type}",
        description=f"**{len(usernames)} attendees** will each receive **{ep} EP**\n*(Source: {source})*",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Attendees", value=f"```\n{lines or 'None'}\n```", inline=False)
    embed.set_footer(text="Review the list and confirm, edit, or cancel.")
    return embed


def _result_embed(entry: dict, host: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"📅 Event Logged — {entry['event_type']}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Host", value=host.mention, inline=True)
    embed.add_field(name="Attendees", value=str(entry["participant_count"]), inline=True)
    embed.add_field(name="EP Awarded", value=f"{entry['ep_awarded']} per player", inline=True)
    embed.add_field(name="Total EP", value=str(entry["ep_awarded"] * entry["participant_count"]), inline=True)
    if entry.get("note"):
        embed.add_field(name="Note", value=entry["note"], inline=False)
    attendees_str = ", ".join(entry["participants"][:20])
    if len(entry["participants"]) > 20:
        attendees_str += f" ... (+{len(entry['participants'])-20} more)"
    embed.add_field(name="Players", value=attendees_str or "None", inline=False)
    embed.timestamp = discord.utils.utcnow()
    return embed


async def setup(bot):
    await bot.add_cog(LogEventCog(bot))
