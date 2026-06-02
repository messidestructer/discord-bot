"""
Event logging cog.
/log — logs an event and awards EP to attendees.

KEY RULE: EP is ONLY awarded to Discord members who are @mentioned AND have a
verified Roblox account. Users without a linked account are listed but receive
no EP and the host is warned about each skipped member.
"""
import json
import os
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db

LOG_CHANNEL_ID        = int(os.getenv("LOG_CHANNEL_ID", "0"))
EP_MANAGER_ROLE_ID    = int(os.getenv("EP_MANAGER_ROLE_ID", "0"))
EVENTS_CONFIG_PATH    = os.getenv("EVENTS_CONFIG_PATH", "events_config.json")

_DEFAULT_EVENTS = [
    {"name": "Training",  "ep": 2},
    {"name": "Patrol",    "ep": 1},
    {"name": "Tryout",    "ep": 3},
    {"name": "Joint Op",  "ep": 4},
    {"name": "Meeting",   "ep": 1},
]

# Rate limit: max 3 event logs per 5 minutes per user
_rate_buckets: dict[int, list[float]] = defaultdict(list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_events() -> list[dict]:
    if os.path.exists(EVENTS_CONFIG_PATH):
        with open(EVENTS_CONFIG_PATH) as f:
            return json.load(f)
    return _DEFAULT_EVENTS


def _check_rate_limit(user_id: int) -> bool:
    now    = time.monotonic()
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
    return [
        app_commands.Choice(name=f"{e['name']} ({e['ep']} EP)", value=e["name"])
        for e in events[:25]
    ]


async def _resolve_attendees(
    guild: discord.Guild,
    members: list[discord.Member],
) -> tuple[list[tuple[discord.Member, str]], list[discord.Member]]:
    """
    For each Discord member, look up their verified Roblox username.
    Returns:
        verified   — list of (member, roblox_username) tuples
        unverified — list of members with no linked Roblox account
    """
    verified:   list[tuple[discord.Member, str]] = []
    unverified: list[discord.Member]             = []
    for member in members:
        roblox = await db.get_roblox_username(member.id)
        if roblox:
            verified.append((member, roblox))
        else:
            unverified.append(member)
    return verified, unverified


# ── Cog ────────────────────────────────────────────────────────────────────────

class LogEventCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="log",
        description="Log an event and award EP. @mention every attendee — unverified members are skipped.",
    )
    @app_commands.describe(
        event_type="Type of event",
        attendees="@mention every attendee (e.g. @Alice @Bob @Charlie)",
        note="Optional note about the event",
        screenshot="Optional screenshot for record-keeping",
    )
    @app_commands.choices(event_type=_event_choices())
    async def log_event(
        self,
        interaction: discord.Interaction,
        event_type: str,
        attendees: str,
        note: str = "",
        screenshot: discord.Attachment = None,
    ):
        if not _has_log_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return
        if not _check_rate_limit(interaction.user.id):
            await interaction.response.send_message(
                "⏳ Rate limited — max 3 event logs per 5 minutes.", ephemeral=True
            )
            return

        events    = _load_events()
        event_cfg = next((e for e in events if e["name"] == event_type), None)
        if not event_cfg:
            await interaction.response.send_message(f"❌ Unknown event type: **{event_type}**", ephemeral=True)
            return

        # Parse @mentions from the attendees string
        mentioned: list[discord.Member] = []
        for mid in {int(m) for m in __import__("re").findall(r"<@!?(\d+)>", attendees)}:
            m = interaction.guild.get_member(mid)
            if m:
                mentioned.append(m)

        if not mentioned:
            await interaction.response.send_message(
                "❌ No valid @mentions found. You must **@mention** every attendee — "
                "EP cannot be awarded without a linked Discord account.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        verified, unverified = await _resolve_attendees(interaction.guild, mentioned)

        if not verified:
            unverified_list = ", ".join(m.mention for m in unverified)
            await interaction.followup.send(
                f"❌ None of the mentioned members have a verified Roblox account.\n"
                f"**Unverified:** {unverified_list}\n\n"
                "Ask them to run `/verify` first, then re-log this event.",
                ephemeral=True,
            )
            return

        ep_per_player = event_cfg["ep"]
        view  = ConfirmLogView(
            event_type, ep_per_player, verified, unverified,
            note, interaction.user, interaction.guild, screenshot,
        )
        embed = _preview_embed(event_type, ep_per_player, verified, unverified)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ── Views ──────────────────────────────────────────────────────────────────────

class EditAttendeesModal(discord.ui.Modal, title="Edit Attendees"):
    mentions = discord.ui.TextInput(
        label="Attendees",
        style=discord.TextStyle.paragraph,
        placeholder="@Alice @Bob @Charlie",
        required=True,
        max_length=2000,
    )

    def __init__(self, original_verified, original_unverified, confirm_view):
        super().__init__()
        self.confirm_view = confirm_view

        try:
            self.mentions.default = " ".join(
                f"<@{m.id}>" for m, _ in original_verified
            )
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        import re
        ids      = {int(mid) for mid in re.findall(r"<@!?(\d+)>", self.mentions.value)}
        members  = [m for mid in ids if (m := interaction.guild.get_member(mid))]

        if not members:
            await interaction.response.send_message("❌ No valid @mentions found.", ephemeral=True)
            return

        verified, unverified = await _resolve_attendees(interaction.guild, members)
        self.confirm_view.verified   = verified
        self.confirm_view.unverified = unverified

        embed = _preview_embed(
            self.confirm_view.event_type,
            self.confirm_view.ep_per_player,
            verified,
            unverified,
        )
        await interaction.response.edit_message(embed=embed, view=self.confirm_view)


class ConfirmLogView(discord.ui.View):
    def __init__(self, event_type, ep_per_player, verified, unverified, note, host, guild, screenshot):
        super().__init__(timeout=300)
        self.event_type    = event_type
        self.ep_per_player = ep_per_player
        self.verified      = verified      # list[tuple[Member, str]]
        self.unverified    = unverified    # list[Member]
        self.note          = note
        self.host          = host
        self.guild         = guild
        self.screenshot    = screenshot

    @discord.ui.button(label="✅ Confirm & Award EP", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        self.stop()

        # Award EP only to verified members
        for member, roblox_username in self.verified:
            await db.set_ep(
                roblox_username, self.ep_per_player,
                self.host.id, f"Event: {self.event_type}",
            )

        roblox_names = [roblox for _, roblox in self.verified]
        entry = await db.log_event(
            event_type       = self.event_type,
            ep_awarded       = self.ep_per_player,
            participants     = roblox_names,
            host_discord_id  = self.host.id,
            host_name        = str(self.host),
            note             = self.note,
        )

        embed = _result_embed(entry, self.verified, self.unverified, self.host)

        # Post screenshot if provided
        files = []
        if self.screenshot:
            data  = await self.screenshot.read()
            files = [discord.File(
                __import__("io").BytesIO(data),
                filename=self.screenshot.filename,
            )]

        log_ch_id = int(os.getenv("EVENT_LOG_CHANNEL_ID") or LOG_CHANNEL_ID or 0)
        if log_ch_id and self.guild:
            ch = self.guild.get_channel(log_ch_id) or await self.guild.fetch_channel(log_ch_id)
            if ch:
                await ch.send(embed=embed, files=files)

        skip_note = ""
        if self.unverified:
            lines = [m.mention for m in self.unverified[:20]]

            if len(self.unverified) > 20:
                lines.append(f"...and {len(self.unverified) - 20} more")

            skip_note = (
                f"\n\n⚠️ **{len(self.unverified)} skipped (no Roblox account)**:\n"
                + "\n".join(lines)
            )

        await interaction.followup.send(
            f"✅ Logged **{self.event_type}** — "
            f"**{len(self.verified)}** member(s) each received **{self.ep_per_player} EP**."
            + skip_note,
            ephemeral=True,
        )

    @discord.ui.button(label="✏️ Edit Attendees", style=discord.ButtonStyle.secondary)
    async def edit_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        modal = EditAttendeesModal(self.verified, self.unverified, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Event log cancelled.", embed=None, view=None)


# ── Embed builders ─────────────────────────────────────────────────────────────

def _preview_embed(
    event_type: str,
    ep: int,
    verified: list[tuple[discord.Member, str]],
    unverified: list[discord.Member],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 Preview — {event_type}",
        color=discord.Color.orange(),
    )

    if verified:
        lines = [f"{m.mention} ({roblox})" for m, roblox in verified[:30]]
        if len(verified) > 30:
            lines.append(f"*...and {len(verified) - 30} more*")
        embed.add_field(
            name=f"✅ Will receive {ep} EP ({len(verified)} members)",
            value="\n".join(lines) or "None",
            inline=False,
        )

    if unverified:
        lines = [m.mention for m in unverified]
        embed.add_field(
            name=f"⚠️ Skipped — no Roblox account ({len(unverified)} members)",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="Review, then confirm, edit, or cancel.")
    return embed


def _result_embed(
    entry: dict,
    verified: list[tuple[discord.Member, str]],
    unverified: list[discord.Member],
    host: discord.Member,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📅 Event Logged — {entry['event_type']}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Host",       value=host.mention,                    inline=True)
    embed.add_field(name="Attendees",  value=str(entry["participant_count"]), inline=True)
    embed.add_field(name="EP Awarded", value=f"{entry['ep_awarded']} each",   inline=True)
    embed.add_field(
        name="Total EP Distributed",
        value=str(entry["ep_awarded"] * entry["participant_count"]),
        inline=True,
    )
    if entry.get("note"):
        embed.add_field(name="Note", value=entry["note"], inline=False)

    # List verified recipients with mentions
    if verified:
        recipient_lines = [f"{m.mention} ({roblox})" for m, roblox in verified[:20]]
        if len(verified) > 20:
            recipient_lines.append(f"*(+{len(verified) - 20} more)*")
        embed.add_field(name="Recipients", value="\n".join(recipient_lines), inline=False)

    # Warn about skipped members
    if unverified:
        embed.add_field(
            name="⚠️ Skipped (no Roblox account)",
            value=", ".join(m.mention for m in unverified),
            inline=False,
        )

    embed.timestamp = discord.utils.utcnow()
    return embed


async def setup(bot):
    await bot.add_cog(LogEventCog(bot))