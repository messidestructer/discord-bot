"""
Event logging cog.

/log      — log an event + award EP.
            • Screenshot → OCR auto-reads names → match to verified Discord members
            • "Add users in VC" button pulls everyone in your current voice channel
            • Interactive attendee editor before confirming
/editlog  — edit an already-logged event (add/remove attendees, change note, fix EP)

KEY RULE: EP is ONLY awarded to Discord members who are verified (have a linked
Roblox account). Unverified members are listed but receive no EP.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.ocr import extract_usernames_from_image

log = logging.getLogger("log_event")

LOG_CHANNEL_ID     = int(os.getenv("LOG_CHANNEL_ID", "0"))
EP_MANAGER_ROLE_ID = int(os.getenv("EP_MANAGER_ROLE_ID", "0"))
EVENTS_CONFIG_PATH = os.getenv("EVENTS_CONFIG_PATH", "events_config.json")

_DEFAULT_EVENTS = [
    {"name": "Training",  "ep": 2},
    {"name": "Patrol",    "ep": 1},
    {"name": "Tryout",    "ep": 3},
    {"name": "Joint Op",  "ep": 4},
    {"name": "Meeting",   "ep": 1},
]

# Rate limit: 3 event logs per 5 minutes per user
_rate_buckets: dict[int, list[float]] = defaultdict(list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_events() -> list[dict]:
    if os.path.exists(EVENTS_CONFIG_PATH):
        try:
            with open(EVENTS_CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
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


def _parse_mentions(text: str, guild: discord.Guild) -> list[discord.Member]:
    """Parse @mention strings into a deduplicated list of Members."""
    members = []
    seen = set()
    for mid in re.findall(r"<@!?(\d+)>", text):
        uid = int(mid)
        if uid not in seen:
            m = guild.get_member(uid)
            if m:
                members.append(m)
                seen.add(uid)
    return members


async def _resolve_members(
    members: list[discord.Member],
) -> tuple[list[tuple[discord.Member, str]], list[discord.Member]]:
    """
    Split members into verified (Member, roblox_username) and unverified.
    """
    verified:   list[tuple[discord.Member, str]] = []
    unverified: list[discord.Member]             = []
    for m in members:
        roblox = await db.get_roblox_username(m.id)
        if roblox:
            verified.append((m, roblox))
        else:
            unverified.append(m)
    return verified, unverified


async def _ocr_to_members(
    image_bytes: bytes,
    guild: discord.Guild,
) -> tuple[list[discord.Member], list[str]]:
    """
    Run OCR on an image, then try to match each name to a verified Discord member.
    Returns (matched_members, unmatched_names).
    """
    ocr_names = extract_usernames_from_image(image_bytes)
    if not ocr_names:
        return [], []

    # Build lookup: roblox_username.lower() -> Member
    data    = await db.load()
    rbx_map: dict[str, discord.Member] = {}
    for discord_id_str, rblx_name in data["verified_users"].items():
        member = guild.get_member(int(discord_id_str))
        if member:
            rbx_map[rblx_name.lower()] = member

    matched:   list[discord.Member] = []
    unmatched: list[str]            = []
    seen_ids: set[int]              = set()

    for name in ocr_names:
        member = rbx_map.get(name.lower())
        if member and member.id not in seen_ids:
            matched.append(member)
            seen_ids.add(member.id)
        else:
            unmatched.append(name)

    return matched, unmatched


# ── Cog ────────────────────────────────────────────────────────────────────────

class LogEventCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /log ──────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="log",
        description="Log an event and award EP. @mention attendees, upload a screenshot, or pull from VC.",
    )
    @app_commands.describe(
        event_type  = "Type of event",
        attendees   = "@mention every attendee (optional if using screenshot or VC pull)",
        note        = "Optional note about the event",
        screenshot  = "Optional screenshot — OCR will auto-read Roblox usernames from it",
    )
    @app_commands.choices(event_type=_event_choices())
    async def log_event(
        self,
        interaction: discord.Interaction,
        event_type : str,
        attendees  : str = "",
        note       : str = "",
        screenshot : discord.Attachment = None,
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
            await interaction.response.send_message(
                f"❌ Unknown event type: **{event_type}**", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        # ── Collect initial members ────────────────────────────────────────
        initial_members: list[discord.Member] = []
        ocr_unmatched:   list[str]            = []
        screenshot_bytes: bytes | None        = None

        # 1. Mentioned members
        if attendees.strip():
            initial_members = _parse_mentions(attendees, interaction.guild)

        # 2. OCR from screenshot
        if screenshot:
            screenshot_bytes = await screenshot.read()
            ocr_matched, ocr_unmatched = await _ocr_to_members(
                screenshot_bytes, interaction.guild
            )
            # Merge, dedup
            existing_ids = {m.id for m in initial_members}
            for m in ocr_matched:
                if m.id not in existing_ids:
                    initial_members.append(m)
                    existing_ids.add(m.id)

        verified, unverified = await _resolve_members(initial_members)

        embed = _preview_embed(
            event_type, event_cfg["ep"], verified, unverified, ocr_unmatched
        )

        view = LogConfirmView(
            event_type    = event_type,
            ep_per_player = event_cfg["ep"],
            verified      = verified,
            unverified    = unverified,
            ocr_unmatched = ocr_unmatched,
            note          = note,
            host          = interaction.user,
            guild         = interaction.guild,
            screenshot_bytes = screenshot_bytes,
            screenshot_name  = screenshot.filename if screenshot else None,
        )

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /editlog ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="editlog",
        description="Edit an already-logged event — change attendees, note, or EP awarded.",
    )
    @app_commands.describe(
        event_index="Event number to edit (1 = most recent). Use /recentevents to find the index.",
        new_note="New note for the event (leave blank to keep existing)",
    )
    @app_commands.default_permissions(manage_roles=True)
    async def edit_log(
        self,
        interaction: discord.Interaction,
        event_index: int = 1,
        new_note: str = "",
    ):
        if not _has_log_permission(interaction):
            await interaction.response.send_message("❌ You need the EP Manager role.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        data = await db.load()
        events_list = data.get("event_log", [])

        if not events_list:
            await interaction.followup.send("❌ No events have been logged yet.", ephemeral=True)
            return

        # event_index 1 = most recent
        idx = len(events_list) - event_index
        if idx < 0 or idx >= len(events_list):
            await interaction.followup.send(
                f"❌ Event #{event_index} not found. There are **{len(events_list)}** logged events.",
                ephemeral=True,
            )
            return

        entry = events_list[idx]
        embed = _event_detail_embed(entry, event_index)

        view = EditLogView(
            db_index   = idx,
            entry      = dict(entry),
            host       = interaction.user,
            guild      = interaction.guild,
            new_note   = new_note or entry.get("note", ""),
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /recentevents ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="recentevents",
        description="List recent logged events with their index numbers for use with /editlog.",
    )
    @app_commands.describe(limit="How many recent events to show (default 10, max 25)")
    async def recent_events(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer(ephemeral=True)
        limit = min(max(limit, 1), 25)

        data = await db.load()
        events_list = data.get("event_log", [])

        if not events_list:
            await interaction.followup.send("No events logged yet.", ephemeral=True)
            return

        lines = []
        # Show most-recent first, numbered from 1
        for i, entry in enumerate(reversed(events_list[-limit:]), 1):
            date = entry.get("logged_at", "?")[:10]
            etype = entry.get("event_type", "?")
            host  = entry.get("host_name", "?")
            count = entry.get("participant_count", 0)
            ep    = entry.get("ep_awarded", 0)
            lines.append(f"`#{i}` **{etype}** — {date} | Host: {host} | {count} attendees × {ep} EP")

        embed = discord.Embed(
            title="📋 Recent Events",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Use /editlog event_index:<number> to edit an event.")
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Views ──────────────────────────────────────────────────────────────────────

class AddMembersModal(discord.ui.Modal, title="Add Members by @mention"):
    mentions = discord.ui.TextInput(
        label="@mention members to add",
        style=discord.TextStyle.paragraph,
        placeholder="@Alice @Bob @Charlie",
        required=True,
        max_length=2000,
    )

    def __init__(self, parent_view: "LogConfirmView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        new_members = _parse_mentions(self.mentions.value, interaction.guild)
        if not new_members:
            await interaction.response.send_message("❌ No valid @mentions found.", ephemeral=True)
            return

        # Merge with existing, dedup
        existing_ids = {m.id for m, _ in self.parent_view.verified}
        existing_ids |= {m.id for m in self.parent_view.unverified}

        to_add = [m for m in new_members if m.id not in existing_ids]
        if not to_add:
            await interaction.response.send_message(
                "ℹ️ All mentioned members are already in the list.", ephemeral=True
            )
            return

        new_ver, new_unver = await _resolve_members(to_add)
        self.parent_view.verified   += new_ver
        self.parent_view.unverified += new_unver

        embed = _preview_embed(
            self.parent_view.event_type,
            self.parent_view.ep_per_player,
            self.parent_view.verified,
            self.parent_view.unverified,
            self.parent_view.ocr_unmatched,
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class RemoveMembersModal(discord.ui.Modal, title="Remove Members"):
    mentions = discord.ui.TextInput(
        label="@mention members to remove",
        style=discord.TextStyle.paragraph,
        placeholder="@Alice @Bob",
        required=True,
        max_length=2000,
    )

    def __init__(self, parent_view: "LogConfirmView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        to_remove_ids = {
            int(mid) for mid in re.findall(r"<@!?(\d+)>", self.mentions.value)
        }
        if not to_remove_ids:
            await interaction.response.send_message("❌ No valid @mentions found.", ephemeral=True)
            return

        self.parent_view.verified   = [(m, r) for m, r in self.parent_view.verified   if m.id not in to_remove_ids]
        self.parent_view.unverified = [m       for m    in self.parent_view.unverified if m.id not in to_remove_ids]

        embed = _preview_embed(
            self.parent_view.event_type,
            self.parent_view.ep_per_player,
            self.parent_view.verified,
            self.parent_view.unverified,
            self.parent_view.ocr_unmatched,
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class EditNoteModal(discord.ui.Modal, title="Edit Event Note"):
    note = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        placeholder="Optional note about this event",
        required=False,
        max_length=500,
    )

    def __init__(self, parent_view: "LogConfirmView"):
        super().__init__()
        self.parent_view = parent_view
        self.note.default = parent_view.note

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.note = self.note.value or ""
        embed = _preview_embed(
            self.parent_view.event_type,
            self.parent_view.ep_per_player,
            self.parent_view.verified,
            self.parent_view.unverified,
            self.parent_view.ocr_unmatched,
        )
        if self.parent_view.note:
            embed.add_field(name="📝 Note", value=self.parent_view.note, inline=False)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class LogConfirmView(discord.ui.View):
    def __init__(
        self,
        event_type:     str,
        ep_per_player:  int,
        verified:       list[tuple[discord.Member, str]],
        unverified:     list[discord.Member],
        ocr_unmatched:  list[str],
        note:           str,
        host:           discord.Member,
        guild:          discord.Guild,
        screenshot_bytes: bytes | None,
        screenshot_name:  str | None,
    ):
        super().__init__(timeout=600)  # 10-minute window
        self.event_type      = event_type
        self.ep_per_player   = ep_per_player
        self.verified        = verified
        self.unverified      = unverified
        self.ocr_unmatched   = ocr_unmatched
        self.note            = note
        self.host            = host
        self.guild           = guild
        self.screenshot_bytes = screenshot_bytes
        self.screenshot_name  = screenshot_name

    def _is_host(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.host.id

    # ── Confirm ────────────────────────────────────────────────────────────

    @discord.ui.button(label="✅ Confirm & Award EP", style=discord.ButtonStyle.green, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can confirm.", ephemeral=True)
            return
        if not self.verified:
            await interaction.response.send_message(
                "❌ No verified attendees — add at least one verified member before confirming.",
                ephemeral=True,
            )
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

        # Award EP
        for member, roblox_username in self.verified:
            await db.set_ep(
                roblox_username,
                self.ep_per_player,
                self.host.id,
                f"Event: {self.event_type}",
            )

        roblox_names = [roblox for _, roblox in self.verified]
        entry = await db.log_event(
            event_type      = self.event_type,
            ep_awarded      = self.ep_per_player,
            participants    = roblox_names,
            host_discord_id = self.host.id,
            host_name       = str(self.host),
            note            = self.note,
        )

        result_embed = _result_embed(entry, self.verified, self.unverified, self.host)

        files = []
        if self.screenshot_bytes:
            files = [discord.File(
                io.BytesIO(self.screenshot_bytes),
                filename=self.screenshot_name or "screenshot.png",
            )]

        log_ch_id = int(os.getenv("EVENT_LOG_CHANNEL_ID") or LOG_CHANNEL_ID or 0)
        if log_ch_id and self.guild:
            try:
                ch = self.guild.get_channel(log_ch_id) or await self.guild.fetch_channel(log_ch_id)
                if ch:
                    await ch.send(embed=result_embed, files=files)
            except Exception as e:
                log.error(f"Failed to post event log: {e}")

        skip_note = ""
        if self.unverified:
            lines = [m.mention for m in self.unverified[:20]]
            if len(self.unverified) > 20:
                lines.append(f"...and {len(self.unverified) - 20} more")
            skip_note = (
                f"\n\n⚠️ **{len(self.unverified)} skipped (no Roblox account):**\n"
                + "\n".join(lines)
            )

        await interaction.followup.send(
            f"✅ Logged **{self.event_type}** — "
            f"**{len(self.verified)}** member(s) each received **{self.ep_per_player} EP**."
            + skip_note,
            ephemeral=True,
        )

    # ── Add via VC ─────────────────────────────────────────────────────────

    @discord.ui.button(label="🔊 Add Users in VC", style=discord.ButtonStyle.blurple, row=0)
    async def add_from_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return

        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.response.send_message(
                "❌ You're not in a voice channel. Join a VC first.", ephemeral=True
            )
            return

        vc_members = [
            m for m in voice_state.channel.members
            if not m.bot
        ]
        if not vc_members:
            await interaction.response.send_message(
                "❌ No non-bot members found in your voice channel.", ephemeral=True
            )
            return

        existing_ids = {m.id for m, _ in self.verified} | {m.id for m in self.unverified}
        to_add = [m for m in vc_members if m.id not in existing_ids]

        if not to_add:
            await interaction.response.send_message(
                "ℹ️ Everyone in your VC is already in the attendee list.", ephemeral=True
            )
            return

        new_ver, new_unver = await _resolve_members(to_add)
        self.verified   += new_ver
        self.unverified += new_unver

        embed = _preview_embed(
            self.event_type, self.ep_per_player,
            self.verified, self.unverified, self.ocr_unmatched,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    # ── Add by mention ──────────────────────────────────────────────────────

    @discord.ui.button(label="➕ Add Members", style=discord.ButtonStyle.secondary, row=1)
    async def add_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(AddMembersModal(self))

    # ── Remove members ──────────────────────────────────────────────────────

    @discord.ui.button(label="➖ Remove Members", style=discord.ButtonStyle.secondary, row=1)
    async def remove_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveMembersModal(self))

    # ── Edit note ────────────────────────────────────────────────────────────

    @discord.ui.button(label="📝 Edit Note", style=discord.ButtonStyle.secondary, row=1)
    async def edit_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(EditNoteModal(self))

    # ── Cancel ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Event log cancelled.", embed=None, view=None)


# ── Edit-log view ──────────────────────────────────────────────────────────────

class EditLogNoteModal(discord.ui.Modal, title="Edit Event Note"):
    note = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, parent_view: "EditLogView"):
        super().__init__()
        self.parent_view = parent_view
        self.note.default = parent_view.entry.get("note", "")

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.entry["note"] = self.note.value or ""
        embed = _event_detail_embed(self.parent_view.entry, None)
        embed.set_footer(text="Note updated — click Save Changes to apply.")
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class AddAttendeesModal(discord.ui.Modal, title="Add Attendees to Event"):
    mentions = discord.ui.TextInput(
        label="@mention members to add",
        style=discord.TextStyle.paragraph,
        placeholder="@Alice @Bob @Charlie",
        required=True,
        max_length=2000,
    )

    def __init__(self, parent_view: "EditLogView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        members = _parse_mentions(self.mentions.value, interaction.guild)
        if not members:
            await interaction.response.send_message("❌ No valid @mentions found.", ephemeral=True)
            return

        ep = self.parent_view.entry.get("ep_awarded", 0)
        added = []
        for m in members:
            roblox = await db.get_roblox_username(m.id)
            if not roblox:
                await interaction.followup.send(
                    f"⚠️ {m.mention} has no verified Roblox account — skipped.", ephemeral=True
                )
                continue
            if roblox.lower() not in [p.lower() for p in self.parent_view.entry.get("participants", [])]:
                self.parent_view.entry.setdefault("participants", []).append(roblox)
                self.parent_view.entry["participant_count"] = len(self.parent_view.entry["participants"])
                # Award EP now
                await db.set_ep(roblox, ep, interaction.user.id, f"Added to event: {self.parent_view.entry.get('event_type', '?')}")
                added.append(roblox)

        embed = _event_detail_embed(self.parent_view.entry, None)
        if added:
            embed.set_footer(text=f"Added: {', '.join(added)} — click Save Changes.")
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class RemoveAttendeesModal(discord.ui.Modal, title="Remove Attendees from Event"):
    names = discord.ui.TextInput(
        label="Roblox usernames to remove (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="PlayerOne\nPlayerTwo",
        required=True,
        max_length=1000,
    )

    def __init__(self, parent_view: "EditLogView"):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        to_remove = {n.strip().lower() for n in self.names.value.splitlines() if n.strip()}
        ep = self.parent_view.entry.get("ep_awarded", 0)
        removed = []
        kept = []
        for p in self.parent_view.entry.get("participants", []):
            if p.lower() in to_remove:
                removed.append(p)
                # Deduct EP
                await db.set_ep(p, -ep, interaction.user.id, f"Removed from event: {self.parent_view.entry.get('event_type', '?')}")
            else:
                kept.append(p)

        self.parent_view.entry["participants"] = kept
        self.parent_view.entry["participant_count"] = len(kept)

        embed = _event_detail_embed(self.parent_view.entry, None)
        if removed:
            embed.set_footer(text=f"Removed: {', '.join(removed)} — click Save Changes.")
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class EditLogView(discord.ui.View):
    def __init__(
        self,
        db_index: int,
        entry:    dict,
        host:     discord.Member,
        guild:    discord.Guild,
        new_note: str,
    ):
        super().__init__(timeout=300)
        self.db_index = db_index
        self.entry    = entry
        self.host     = host
        self.guild    = guild
        if new_note:
            self.entry["note"] = new_note

    def _is_host(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.host.id

    @discord.ui.button(label="💾 Save Changes", style=discord.ButtonStyle.green, row=0)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can save.", ephemeral=True)
            return

        # Write back to database
        async with db._lock:
            data = db._load_raw()
            if 0 <= self.db_index < len(data["event_log"]):
                data["event_log"][self.db_index] = self.entry
                db._save_raw(data)

        for child in self.children:
            child.disabled = True
        self.stop()

        embed = _event_detail_embed(self.entry, None)
        embed.color = discord.Color.green()
        embed.set_footer(text="✅ Changes saved.")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📝 Edit Note", style=discord.ButtonStyle.secondary, row=0)
    async def edit_note(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(EditLogNoteModal(self))

    @discord.ui.button(label="➕ Add Attendees", style=discord.ButtonStyle.secondary, row=1)
    async def add_attendees(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(AddAttendeesModal(self))

    @discord.ui.button(label="➖ Remove Attendees", style=discord.ButtonStyle.secondary, row=1)
    async def remove_attendees(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can edit.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveAttendeesModal(self))

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_host(interaction):
            await interaction.response.send_message("Only the command invoker can cancel.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Edit cancelled — no changes made.", embed=None, view=None)


# ── Embed builders ─────────────────────────────────────────────────────────────

def _preview_embed(
    event_type:    str,
    ep:            int,
    verified:      list[tuple[discord.Member, str]],
    unverified:    list[discord.Member],
    ocr_unmatched: list[str],
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
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="✅ Recipients",
            value="*None yet — add members using the buttons below.*",
            inline=False,
        )

    if unverified:
        lines = [m.mention for m in unverified]
        embed.add_field(
            name=f"⚠️ Skipped — no Roblox account ({len(unverified)} members)",
            value="\n".join(lines),
            inline=False,
        )

    if ocr_unmatched:
        lines = ocr_unmatched[:20]
        if len(ocr_unmatched) > 20:
            lines.append(f"...and {len(ocr_unmatched) - 20} more")
        embed.add_field(
            name=f"🔍 OCR found but not matched to a Discord member ({len(ocr_unmatched)})",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text="Use the buttons below to adjust attendees, then confirm.")
    return embed


def _result_embed(
    entry:      dict,
    verified:   list[tuple[discord.Member, str]],
    unverified: list[discord.Member],
    host:       discord.Member,
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

    if verified:
        lines = [f"{m.mention} ({roblox})" for m, roblox in verified[:20]]
        if len(verified) > 20:
            lines.append(f"*(+{len(verified) - 20} more)*")
        embed.add_field(name="Recipients", value="\n".join(lines), inline=False)

    if unverified:
        embed.add_field(
            name="⚠️ Skipped (no Roblox account)",
            value=", ".join(m.mention for m in unverified[:20]),
            inline=False,
        )

    embed.timestamp = discord.utils.utcnow()
    return embed


def _event_detail_embed(entry: dict, index: int | None) -> discord.Embed:
    """Embed showing full detail of a single logged event, used in /editlog."""
    title = f"📋 Event #{index}" if index else "📋 Event Details"
    embed = discord.Embed(title=title, color=discord.Color.orange())
    embed.add_field(name="Type",        value=entry.get("event_type", "?"),         inline=True)
    embed.add_field(name="Host",        value=entry.get("host_name", "?"),           inline=True)
    embed.add_field(name="EP Awarded",  value=str(entry.get("ep_awarded", 0)),       inline=True)
    embed.add_field(name="Attendees",   value=str(entry.get("participant_count", 0)), inline=True)
    embed.add_field(name="Logged At",   value=entry.get("logged_at", "?")[:16].replace("T", " "), inline=True)
    if entry.get("note"):
        embed.add_field(name="Note", value=entry["note"], inline=False)

    participants = entry.get("participants", [])
    if participants:
        chunk = participants[:30]
        suffix = f"\n...and {len(participants) - 30} more" if len(participants) > 30 else ""
        embed.add_field(
            name="Participants",
            value="\n".join(chunk) + suffix,
            inline=False,
        )
    return embed


async def setup(bot):
    await bot.add_cog(LogEventCog(bot))