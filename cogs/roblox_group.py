"""
Roblox group management commands.
/rank        — set a member's rank
/kickroblox  — kick from group
/joinrequest — view/accept/deny pending join requests
/grouproles  — list all group roles
"""
import logging
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
    _extract_requester,
)

log = logging.getLogger("roblox_group")

GROUP_ID            = os.getenv("ROBLOX_GROUP_ID", "")
LOG_CHANNEL_ID      = int(os.getenv("LOG_CHANNEL_ID", "0"))
MAX_ASSIGNABLE_RANK = int(os.getenv("MAX_ASSIGNABLE_RANK", "250"))

_roles_cache:      list[dict] = []
_roles_cache_time: float      = 0
_ROLES_TTL = 300

_join_requests_cache:      list[dict] = []
_join_requests_cache_time: float      = 0
_JOIN_REQUESTS_TTL = 30


async def _get_cached_roles() -> list[dict]:
    global _roles_cache, _roles_cache_time
    if time() - _roles_cache_time > _ROLES_TTL:
        fresh = await get_group_roles()
        if fresh is not None:
            _roles_cache      = sorted(fresh, key=lambda r: r["rank"])
            _roles_cache_time = time()
    return _roles_cache


async def _get_cached_join_requests() -> list[dict]:
    global _join_requests_cache, _join_requests_cache_time
    if time() - _join_requests_cache_time > _JOIN_REQUESTS_TTL:
        fresh = await get_join_requests()
        if fresh is not None:
            _join_requests_cache      = fresh
            _join_requests_cache_time = time()
    return _join_requests_cache


def _log_channel(guild: discord.Guild):
    if LOG_CHANNEL_ID:
        return guild.get_channel(LOG_CHANNEL_ID)
    return None


class RobloxGroupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /rank ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="Set a member's Roblox group rank.")
    @app_commands.describe(
        member      = "The Discord member to rank (must be verified)",
        rank_number = "Rank number to assign — start typing to search roles",
    )
    @app_commands.default_permissions(manage_roles=True)
    async def rank(self, interaction: discord.Interaction, member: discord.Member, rank_number: int):
        await interaction.response.defer()

        if member.id == interaction.user.id:
            await interaction.followup.send("❌ You cannot rank yourself.")
            return

        info = await db.get_roblox_info(member.id)
        if not info:
            await interaction.followup.send(
                f"❌ {member.mention} hasn't verified their Roblox account yet. They need to run `/verify` first."
            )
            return

        roblox_username = info.get("roblox_username", "")
        roblox_id       = info.get("roblox_id", 0) or 0

        if rank_number > MAX_ASSIGNABLE_RANK:
            await interaction.followup.send(f"❌ You cannot assign ranks above **{MAX_ASSIGNABLE_RANK}**.")
            return

        roles       = await _get_cached_roles()
        valid_ranks = {r["rank"] for r in roles}
        if rank_number not in valid_ranks:
            await interaction.followup.send(
                f"❌ Rank **{rank_number}** does not exist in this group. Use `/grouproles` to see valid ranks."
            )
            return

        # Resolve roblox_id if needed
        if not roblox_id:
            roblox_id = await get_user_id_by_name(roblox_username) or 0

        if not roblox_id:
            await interaction.followup.send(f"❌ Couldn't find Roblox user **{roblox_username}**.")
            return

        old_rank = await get_group_rank(roblox_id)
        if old_rank == rank_number:
            await interaction.followup.send(
                f"ℹ️ **{roblox_username}** already has rank **{rank_number}** — no change made."
            )
            return

        old_role = await get_group_role_name(roblox_id)
        success  = await set_group_rank_by_number(roblox_id, rank_number)

        if not success:
            await interaction.followup.send(
                f"❌ Failed to set rank **{rank_number}** for **{roblox_username}**. "
                "Make sure the bot account's rank is higher than the target rank."
            )
            return

        new_role = await get_group_role_name(roblox_id)

        embed = discord.Embed(title="✅ Rank Updated", color=discord.Color.green())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member",   value=member.mention,                            inline=True)
        embed.add_field(name="Roblox",   value=roblox_username,                           inline=True)
        embed.add_field(name="Old Rank", value=f"{old_role or '?'} (#{old_rank or '?'})", inline=True)
        embed.add_field(name="New Rank", value=f"{new_role or '?'} (#{rank_number})",     inline=True)
        embed.add_field(name="Set by",   value=interaction.user.mention,                  inline=True)
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed)

        try:
            await db.log_rank_change(roblox_username, old_rank, rank_number, interaction.user.id)
        except Exception as e:
            log.warning(f"Failed to log rank change: {e}")

        try:
            from cogs.role_binds import sync_member_roles
            await sync_member_roles(member, roblox_username)
        except Exception as e:
            log.warning(f"Role bind sync failed after /rank: {e}")

        ch = _log_channel(interaction.guild)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

    @rank.autocomplete("rank_number")
    async def rank_number_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        roles   = await _get_cached_roles()
        choices = []
        for r in roles:
            label = f"#{r['rank']} — {r['name']}"
            if not current or current.lower() in label.lower() or current == str(r["rank"]):
                choices.append(app_commands.Choice(name=label, value=r["rank"]))
            if len(choices) >= 25:
                break
        return choices

    # ── /grouproles ───────────────────────────────────────────────────────────

    @app_commands.command(name="grouproles", description="List all roles in the Roblox group.")
    async def grouproles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        roles = await _get_cached_roles()
        if not roles:
            await interaction.followup.send(
                "❌ Could not fetch group roles. Check that `ROBLOX_GROUP_ID` and `ROBLOX_COOKIE` are set correctly.",
                ephemeral=True,
            )
            return

        lines = [f"**#{r['rank']}** — {r['name']}" for r in roles]
        embed = discord.Embed(
            title       = f"Group Roles (ID: {GROUP_ID})",
            description = "\n".join(lines),
            color       = discord.Color.blue(),
        )
        embed.set_footer(text=f"{len(roles)} roles total")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /kickroblox ───────────────────────────────────────────────────────────

    @app_commands.command(name="kickroblox", description="Kick a member from the Roblox group.")
    @app_commands.describe(member="The Discord member to kick from the group (must be verified)")
    @app_commands.default_permissions(manage_guild=True)
    async def kickroblox(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        if member.id == interaction.user.id:
            await interaction.followup.send("❌ You cannot kick yourself.")
            return

        info = await db.get_roblox_info(member.id)
        if not info:
            await interaction.followup.send(
                f"❌ {member.mention} hasn't verified their Roblox account — cannot determine their group membership."
            )
            return

        roblox_username = info.get("roblox_username", "")
        roblox_id       = info.get("roblox_id", 0) or 0

        if not roblox_id:
            roblox_id = await get_user_id_by_name(roblox_username) or 0

        if not roblox_id:
            await interaction.followup.send(f"❌ Couldn't resolve Roblox user **{roblox_username}**.")
            return

        success = await kick_from_group(roblox_id)
        if success:
            embed = discord.Embed(title="🚪 Kicked from Roblox Group", color=discord.Color.red())
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Member",    value=member.mention,           inline=True)
            embed.add_field(name="Roblox",    value=roblox_username,          inline=True)
            embed.add_field(name="Kicked by", value=interaction.user.mention, inline=True)
            embed.timestamp = discord.utils.utcnow()
            await interaction.followup.send(embed=embed)
            ch = _log_channel(interaction.guild)
            if ch:
                await ch.send(embed=embed)
        else:
            await interaction.followup.send(
                f"❌ Failed to kick **{roblox_username}** from the group. "
                "They may not be in the group, or the bot lacks permission."
            )

    # ── /joinrequest ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="joinrequest",
        description="View, accept, or deny Roblox group join requests.",
    )
    @app_commands.describe(
        operation = "What to do with pending requests",
        username  = "Specific Roblox username to accept/deny (autocomplete from pending list)",
    )
    @app_commands.choices(operation=[
        app_commands.Choice(name="View pending requests",  value="view"),
        app_commands.Choice(name="Accept — specific user", value="accept_one"),
        app_commands.Choice(name="Accept — all pending",   value="accept_all"),
        app_commands.Choice(name="Deny — specific user",   value="deny_one"),
        app_commands.Choice(name="Deny — all pending",     value="deny_all"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def joinrequest(
        self,
        interaction: discord.Interaction,
        operation:   str,
        username:    str = None,
    ):
        await interaction.response.defer(ephemeral=(operation == "view"))

        # Always do a fresh fetch — don't use stale cache for writes
        requests = await get_join_requests()
        # Update cache
        global _join_requests_cache, _join_requests_cache_time
        _join_requests_cache      = requests or []
        _join_requests_cache_time = time()

        if operation == "view":
            if not requests:
                await interaction.followup.send("✅ No pending join requests.", ephemeral=True)
                return
            view  = JoinRequestsView(requests, interaction.user.id)
            embed = _requests_embed(requests)
            msg   = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
            view.message = msg
            return

        # Single-user operations
        if operation in ("accept_one", "deny_one"):
            if not username:
                await interaction.followup.send(
                    "❌ Please provide the username of the specific user you want to act on.",
                    ephemeral=True,
                )
                return

            # Find the request by username
            target_req = None
            for req in requests:
                uid, uname = _extract_requester(req)
                if uname.lower() == username.lower():
                    target_req = (uid, uname)
                    break

            if not target_req:
                await interaction.followup.send(
                    f"❌ **{username}** does not have a pending join request.", ephemeral=True
                )
                return

            uid, uname = target_req
            if not uid:
                await interaction.followup.send(f"❌ Could not determine Roblox ID for **{uname}**.", ephemeral=True)
                return

            if operation == "accept_one":
                ok   = await accept_join_request(uid)
                verb = "Accepted"
            else:
                ok   = await deny_join_request(uid)
                verb = "Denied"

            if ok:
                await interaction.followup.send(f"✅ {verb} join request for **{uname}**.")
            else:
                await interaction.followup.send(
                    f"❌ Failed — **{uname}** may no longer have a pending request."
                )
            return

        # Bulk operations
        if not requests:
            await interaction.followup.send("✅ No pending join requests.", ephemeral=True)
            return

        success_count = 0
        fail_count    = 0
        for req in requests:
            uid, uname = _extract_requester(req)
            if not uid:
                fail_count += 1
                continue
            ok = await (accept_join_request(uid) if operation == "accept_all" else deny_join_request(uid))
            if ok:
                success_count += 1
            else:
                fail_count += 1

        verb = "Accepted" if operation == "accept_all" else "Denied"
        msg  = f"✅ {verb} **{success_count}/{len(requests)}** join requests."
        if fail_count:
            msg += f" ({fail_count} failed)"
        await interaction.followup.send(msg)

    @joinrequest.autocomplete("username")
    async def username_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        requests = await _get_cached_join_requests()
        choices  = []
        for req in requests:
            _, name = _extract_requester(req)
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
        self.message  = None

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
        await interaction.response.edit_message(view=self)
        self.stop()
        count = 0
        for req in self.requests:
            uid, _ = _extract_requester(req)
            if uid and await accept_join_request(uid):
                count += 1
        await interaction.followup.send(f"✅ Accepted **{count}/{len(self.requests)}** join requests.")

    @discord.ui.button(label="❌ Deny All", style=discord.ButtonStyle.red)
    async def deny_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        count = 0
        for req in self.requests:
            uid, _ = _extract_requester(req)
            if uid and await deny_join_request(uid):
                count += 1
        await interaction.followup.send(f"✅ Denied **{count}/{len(self.requests)}** join requests.")


def _requests_embed(requests: list) -> discord.Embed:
    lines = []
    for req in requests[:20]:
        uid, uname = _extract_requester(req)
        lines.append(f"• **{uname}** (ID: `{uid}`)")
    if len(requests) > 20:
        lines.append(f"*...and {len(requests) - 20} more*")
    return discord.Embed(
        title       = f"📋 Pending Join Requests — {len(requests)} total",
        description = "\n".join(lines) or "None",
        color       = discord.Color.orange(),
    )


async def setup(bot):
    await bot.add_cog(RobloxGroupCog(bot))