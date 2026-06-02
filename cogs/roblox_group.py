"""
Roblox group management commands.
/rank          — set a member's rank (rank choices auto-loaded from the group)
/kickroblox    — kick a member from the group
/joinrequest   — view, accept, or deny pending join requests (auto-fetches the list)
/grouproles    — list all group roles
"""
import os
from time import time

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

GROUP_ID       = os.getenv("ROBLOX_GROUP_ID", "")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

MAX_ASSIGNABLE_RANK = int(
    os.getenv("MAX_ASSIGNABLE_RANK", "250")
)

# Cache group roles for 5 minutes to avoid hammering the API on autocomplete
_roles_cache:      list[dict] = []
_roles_cache_time: float      = 0
_ROLES_TTL = 300

_join_requests_cache = []
_join_requests_cache_time = 0
_JOIN_REQUESTS_TTL = 30

async def _get_cached_join_requests():
    global _join_requests_cache
    global _join_requests_cache_time

    if time() - _join_requests_cache_time > _JOIN_REQUESTS_TTL:
        fresh = await get_join_requests()

        if fresh is not None:
            _join_requests_cache = fresh
        _join_requests_cache_time = time()

    return _join_requests_cache

async def _get_cached_roles() -> list[dict]:
    global _roles_cache, _roles_cache_time
    if time() - _roles_cache_time > _ROLES_TTL:
        fresh = await get_group_roles()
        if fresh is not None:
            _roles_cache      = sorted(fresh, key=lambda r: r["rank"])
            _roles_cache_time = time()
    return _roles_cache


def _log_channel(guild: discord.Guild):
    if LOG_CHANNEL_ID:
        return guild.get_channel(LOG_CHANNEL_ID)
    return None


class RobloxGroupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /rank — with live autocomplete from the group ─────────────────────────

    @app_commands.command(name="rank", description="Set a member's Roblox group rank.")
    @app_commands.describe(
        member="The Discord member to rank (must be verified)",
        rank_number="Rank number to assign — use /grouproles to browse",
    )
    @app_commands.default_permissions(manage_roles=True)
    async def rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank_number: int,
    ):
        await interaction.response.defer()

        if member.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You cannot rank yourself.",
            )
            return

        roblox_username = await db.get_roblox_username(member.id)
        if not roblox_username:
            await interaction.followup.send(
                f"❌ {member.mention} hasn't verified their Roblox account yet. "
                "They need to run `/verify` first.",
            )
            return

        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(
                f"❌ Couldn't find Roblox user **{roblox_username}**."
            )
            return
        
        if rank_number > MAX_ASSIGNABLE_RANK:
            await interaction.followup.send(
                f"❌ You cannot assign ranks above {MAX_ASSIGNABLE_RANK}."
            )
            return
        
        roles = await _get_cached_roles()

        valid_ranks = {
            role["rank"]
            for role in roles
        }

        if rank_number not in valid_ranks:
            await interaction.followup.send(
                f"❌ Rank {rank_number} does not exist."
            )
            return

        old_rank = await get_group_rank(roblox_id)
        if old_rank == rank_number:
            await interaction.followup.send(
                f"❌ {roblox_username} already has that rank."
            )
            return

        old_role = await get_group_role_name(roblox_id)
        success  = await set_group_rank_by_number(roblox_id, rank_number)

        if not success:
            await interaction.followup.send(
                f"❌ Failed to set rank **{rank_number}**. "
                "Make sure the rank exists and the bot account has permission."
            )
            return

        new_role = await get_group_role_name(roblox_id)
        embed = discord.Embed(title="✅ Rank Updated", color=discord.Color.green())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member",   value=member.mention,                              inline=True)
        embed.add_field(name="Roblox",   value=roblox_username,                             inline=True)
        embed.add_field(name="Old Rank", value=f"{old_role or '?'} (#{old_rank or '?'})", inline=True)
        embed.add_field(name="New Rank", value=f"{new_role or '?'} (#{rank_number})",     inline=True)
        embed.add_field(name="Set by",   value=interaction.user.mention,                   inline=True)
        await interaction.followup.send(embed=embed)

        try:
            await db.log_rank_change(
                roblox_username,
                old_rank,
                rank_number,
                interaction.user.id,
            )
        except Exception:
            pass

        log_ch = _log_channel(interaction.guild)
        if log_ch:
            await log_ch.send(embed=embed)

    @rank.autocomplete("rank_number")
    async def rank_number_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        roles = await _get_cached_roles()
        choices = []
        for r in roles:
            label = f"#{r['rank']} — {r['name']}"
            if not current or current.lower() in label.lower() or current == str(r["rank"]):
                choices.append(app_commands.Choice(name=label, value=r["rank"]))
            if len(choices) >= 25:
                break
        return choices

    # ── /grouproles ──────────────────────────────────────────────────────────

    @app_commands.command(name="grouproles", description="List all roles in the Roblox group.")
    async def grouproles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        roles = await _get_cached_roles()
        if not roles:
            await interaction.followup.send("❌ Could not fetch group roles.", ephemeral=True)
            return
        lines = [f"**#{r['rank']}** — {r['name']}" for r in roles]
        embed = discord.Embed(
            title=f"Group Roles (ID: {GROUP_ID})",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /kickroblox ──────────────────────────────────────────────────────────

    @app_commands.command(name="kickroblox", description="Kick a member from the Roblox group.")
    @app_commands.describe(member="The Discord member to kick from the group (must be verified)")
    @app_commands.default_permissions(manage_guild=True)
    async def kickroblox(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        if member.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You cannot kick yourself.",
            )
            return

        roblox_username = await db.get_roblox_username(member.id)
        if not roblox_username:
            await interaction.followup.send(
                f"❌ {member.mention} hasn't verified their Roblox account — cannot determine their group membership."
            )
            return

        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(f"❌ Couldn't resolve Roblox user **{roblox_username}**.")
            return

        success = await kick_from_group(roblox_id)
        if success:
            embed = discord.Embed(
                title="🚪 Kicked from Roblox Group",
                color=discord.Color.red(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Member",    value=member.mention,             inline=True)
            embed.add_field(name="Roblox",    value=roblox_username,            inline=True)
            embed.add_field(name="Kicked by", value=interaction.user.mention,   inline=True)
            await interaction.followup.send(embed=embed)
            log_ch = _log_channel(interaction.guild)
            if log_ch:
                await log_ch.send(embed=embed)
        else:
            await interaction.followup.send(
                f"❌ Failed to kick **{roblox_username}**. "
                "They may not be in the group, or the bot lacks permission."
            )

    # ── /joinrequest ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="joinrequest",
        description="View, accept, or deny Roblox group join requests. The list loads automatically.",
    )
    @app_commands.describe(
        operation="What to do with pending requests",
        username="Specific username to accept/deny (leave blank to see the full list or act on all)",
    )
    @app_commands.choices(operation=[
        app_commands.Choice(name="View pending requests", value="view"),
        app_commands.Choice(name="Accept — specific user", value="accept_one"),
        app_commands.Choice(name="Accept — all pending",   value="accept_all"),
        app_commands.Choice(name="Deny — specific user",   value="deny_one"),
        app_commands.Choice(name="Deny — all pending",     value="deny_all"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def joinrequest(
        self,
        interaction: discord.Interaction,
        operation: str,
        username: str = None,
    ):
        await interaction.response.defer(ephemeral=(operation == "view"))

        # Auto-fetch pending requests
        requests = await get_join_requests()

        if operation == "view":
            if not requests:
                await interaction.followup.send("✅ No pending join requests.", ephemeral=True)
                return
            view  = JoinRequestsView(requests, interaction.user.id)
            embed = _requests_embed(requests)
            msg = await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=True,
                wait=True,
            )

            view.message = msg
            return

        # Single-user operations
        if operation in ("accept_one", "deny_one"):
            if not username:
                await interaction.followup.send(
                    "❌ Please provide the **username** of the specific user you want to act on.",
                    ephemeral=True,
                )
                return
            
            pending_names = {
                req.get("requester", {})
                .get("username", "")
                .lower()
                for req in requests
            }

            if username.lower() not in pending_names:
                await interaction.followup.send(
                    f"❌ {username} does not currently have a pending join request."
                )
                return

            roblox_id = await get_user_id_by_name(username)
            if not roblox_id:
                await interaction.followup.send(f"❌ Roblox user **{username}** not found.")
                return

            if operation == "accept_one":
                ok = await accept_join_request(roblox_id)
                verb = "Accepted"
            else:
                ok = await deny_join_request(roblox_id)
                verb = "Denied"

            if ok:
                await interaction.followup.send(f"✅ {verb} join request for **{username}**.")
            else:
                await interaction.followup.send(
                    f"❌ Failed. **{username}** may not have a pending request."
                )
            return

        # Bulk operations
        if not requests:
            await interaction.followup.send("✅ No pending join requests.")
            return

        success = []
        failed = []

        for req in requests:
            uid = req.get("requester", {}).get("userId")

            if not uid:
                continue

            if operation == "accept_all":
                ok = await accept_join_request(uid)
            else:
                ok = await deny_join_request(uid)

            if ok:
                success.append(uid)
            else:
                failed.append(uid)

        verb = "Accepted" if operation == "accept_all" else "Denied"

        msg = f"✅ {verb} **{len(success)}/{len(requests)}** join requests."

        if failed:
            msg += f"\n❌ Failed: {len(failed)}"

        await interaction.followup.send(msg)

    @joinrequest.autocomplete("username")
    async def username_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete from live pending-request list."""
        requests = await _get_cached_join_requests()
        choices  = []
        for req in requests:
            name = req.get("requester", {}).get("username", "")
            if not current or current.lower() in name.lower():
                choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= 25:
                break
        return choices


# ── Views ──────────────────────────────────────────────────────────────────────

class JoinRequestsView(discord.ui.View):
    def __init__(self, requests: list, owner_id: int):
        super().__init__(timeout=120)

        self.requests = requests
        self.owner_id = owner_id
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="✅ Accept All", style=discord.ButtonStyle.green)
    async def accept_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            view=self
        )
        success = sum(
            1 for req in self.requests
            if (uid := req.get("requester", {}).get("userId"))
            and await accept_join_request(uid)
        )
        self.stop()
        await interaction.followup.send(f"✅ Accepted **{success}/{len(self.requests)}** join requests.")

    @discord.ui.button(label="❌ Deny All", style=discord.ButtonStyle.red)
    async def deny_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(
            view=self
        )
        success = sum(
            1 for req in self.requests
            if (uid := req.get("requester", {}).get("userId"))
            and await deny_join_request(uid)
        )
        self.stop()
        await interaction.followup.send(f"✅ Denied **{success}/{len(self.requests)}** join requests.")


# ── Embed helpers ──────────────────────────────────────────────────────────────

def _requests_embed(requests: list) -> discord.Embed:
    chunks = requests[:20]
    lines  = []
    for req in chunks:
        uid   = req.get("requester", {}).get("userId", "?")
        uname = req.get("requester", {}).get("username", "Unknown")
        lines.append(f"• **{uname}** (ID: `{uid}`)")
    if len(requests) > 20:
        lines.append(f"*...and {len(requests) - 20} more*")
    return discord.Embed(
        title=f"📋 Pending Join Requests — {len(requests)} total",
        description="\n".join(lines) or "None",
        color=discord.Color.orange(),
    )


async def setup(bot):
    await bot.add_cog(RobloxGroupCog(bot))