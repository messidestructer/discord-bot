"""
Roblox verification cog.

Flow priority:
1. Bloxlink lookup (instant, no code needed) — fires automatically on member join too.
2. Manual code placement in the Roblox profile About section.

on_member_join: silently tries Bloxlink; if found, completes verification automatically.
/verify: same priority, falls back to manual code flow if Bloxlink can't find them.
/bloxlink-sync: force a Bloxlink re-check for yourself.
/reverify: staff reset.
/verify-all: (owner only) bulk-verify every unverified member via Bloxlink.
"""
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import storage.database as db
from utils.roblox_api import (
    bloxlink_get_roblox_id,
    get_profile_description,
    get_user_id_by_name,
    get_user_profile,
)

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

log = logging.getLogger("verify")

VERIFIED_ROLE_ID    = int(os.getenv("VERIFIED_ROLE_ID", "0"))
CODE_EXPIRE_MINUTES = 15


def _make_code() -> str:
    words = [
        "amber", "bravo", "cedar", "delta", "eagle", "falcon", "gamma",
        "hotel", "indigo", "jade", "kilo", "lemon", "maple", "nova",
        "ocean", "pearl", "quest", "rapid", "solar", "tiger", "ultra",
        "valor", "wave", "xenon", "yield", "zeal",
    ]
    return "-".join(random.choices(words, k=4))


async def _assign_verified_role(member: discord.Member, guild: discord.Guild):
    """Assign verified role and set nickname. Silently ignores permission errors."""
    try:
        await member.edit(nick=member.display_name if len(member.display_name) <= 32 else member.display_name[:32])
    except discord.Forbidden:
        pass
    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass


async def _complete_verification(
    member: discord.Member,
    guild: discord.Guild,
    discord_id: int,
    roblox_username: str,
) -> bool:
    """
    Persist verification, set nickname to Roblox username, assign Verified role.
    Also triggers role-bind sync if the role_binds cog is loaded.
    Returns False if the Roblox account is already claimed by another Discord account.
    """
    success = await db.claim_verification(discord_id, roblox_username)
    if not success:
        return False

    m = guild.get_member(discord_id) if not isinstance(member, discord.Member) else member
    if not m:
        return True  # persisted but couldn't update nick/roles — that's fine

    try:
        await m.edit(nick=roblox_username[:32])
    except discord.Forbidden:
        pass

    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role and role not in m.roles:
            try:
                await m.add_roles(role)
            except discord.Forbidden:
                pass

    # Sync role binds (fire-and-forget; import lazily to avoid circular imports)
    try:
        from cogs.role_binds import sync_member_roles
        await sync_member_roles(m, roblox_username)
    except Exception as e:
        log.warning(f"Role bind sync failed for {m}: {e}")

    return True


async def _try_bloxlink(discord_id: int, guild_id: str) -> tuple[int | None, str | None]:
    """
    Returns (roblox_id, roblox_username) if Bloxlink resolves this user, else (None, None).
    """
    bloxlink_key = os.getenv("BLOXLINK_API_KEY")
    if not bloxlink_key:
        return None, None

    roblox_id = await bloxlink_get_roblox_id(discord_id, str(guild_id))
    if not roblox_id:
        return None, None

    profile = await get_user_profile(roblox_id)
    username = profile["name"] if profile else str(roblox_id)
    return roblox_id, username


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── Auto-verify on join ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        When someone joins, silently try Bloxlink. If found and not already
        verified, complete verification automatically and DM them.
        """
        # Already verified?
        existing = await db.get_roblox_username(member.id)
        if existing:
            # Just make sure they have the role
            await _assign_verified_role(member, member.guild)
            return

        roblox_id, roblox_username = await _try_bloxlink(member.id, member.guild.id)
        if not roblox_id:
            return

        # Check if the Roblox account is already claimed by another Discord user
        if await db.is_roblox_verified(roblox_username):
            return

        success = await _complete_verification(member, member.guild, member.id, roblox_username)
        if success:
            log.info(f"Auto-verified {member} as {roblox_username} via Bloxlink on join.")
            try:
                embed = discord.Embed(
                    title="✅ Automatically Verified!",
                    description=(
                        f"Welcome to **{member.guild.name}**!\n\n"
                        f"You've been automatically verified as **{roblox_username}** via Bloxlink.\n"
                        "Your nickname has been updated."
                    ),
                    color=discord.Color.green(),
                )
                await member.send(embed=embed)
            except discord.Forbidden:
                pass  # DMs closed — that's fine

    # ── /verify ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="verify",
        description="Link your Roblox account. Auto-detects via Bloxlink if you've verified there.",
    )
    @app_commands.describe(roblox_username="Your Roblox username (only needed if Bloxlink can't find you)")
    async def verify(self, interaction: discord.Interaction, roblox_username: str = None):
        await interaction.response.defer(ephemeral=True)

        # Already verified?
        existing = await db.get_roblox_username(interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"✅ You're already verified as **{existing}**.\n"
                "Need to change your account? Ask a staff member to use `/reverify`.",
                ephemeral=True,
            )
            return

        # ── Try Bloxlink first ─────────────────────────────────────────────
        roblox_id, bloxlink_username = await _try_bloxlink(
            interaction.user.id, interaction.guild_id
        )

        if roblox_id:
            if await db.is_roblox_verified(bloxlink_username):
                await interaction.followup.send(
                    f"❌ Roblox account **{bloxlink_username}** is already linked to another Discord account.",
                    ephemeral=True,
                )
                return

            success = await _complete_verification(
                interaction.user, interaction.guild, interaction.user.id, bloxlink_username
            )
            if success:
                await interaction.followup.send(
                    f"✅ Verified via **Bloxlink** as **{bloxlink_username}**!\n"
                    "Your nickname has been updated.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Roblox account **{bloxlink_username}** is already linked to another Discord account.",
                    ephemeral=True,
                )
            return

        # ── Manual fallback ────────────────────────────────────────────────
        notice = (
            "ℹ️ Bloxlink couldn't find your account automatically.\n\n"
            if os.getenv("BLOXLINK_API_KEY") else ""
        )

        if not roblox_username:
            await interaction.followup.send(
                f"{notice}Please provide your Roblox username:\n`/verify roblox_username:YourName`",
                ephemeral=True,
            )
            return

        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(
                f"❌ Roblox user **{roblox_username}** not found. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        profile   = await get_user_profile(roblox_id)
        canonical = profile.get("name", roblox_username) if profile else roblox_username

        if await db.is_roblox_verified(canonical):
            await interaction.followup.send(
                f"❌ Roblox account **{canonical}** is already linked to another Discord account.",
                ephemeral=True,
            )
            return

        code       = _make_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=CODE_EXPIRE_MINUTES)).isoformat()
        await db.store_pending_verification(interaction.user.id, canonical, code, expires_at)

        embed = discord.Embed(
            title="🔗 Roblox Verification",
            color=discord.Color.blue(),
            description=(
                f"To verify as **{canonical}**, paste the code below into the "
                f"**About** section of your "
                f"[Roblox profile](https://www.roblox.com/users/{roblox_id}/profile).\n\n"
                f"```\n{code}\n```\n"
                f"Then click **Confirm** below.\n"
                f"⏱️ Code expires in **{CODE_EXPIRE_MINUTES} minutes**."
            ),
        )
        embed.set_footer(text=f"Roblox ID: {roblox_id}")

        view = VerifyConfirmView(interaction.user.id, roblox_id, canonical, code, self.bot)
        await interaction.followup.send(f"{notice}", embed=embed, view=view, ephemeral=True)

    # ── /bloxlink-sync ────────────────────────────────────────────────────────

    @app_commands.command(
        name="bloxlink-sync",
        description="Re-check Bloxlink to verify or update your linked Roblox account.",
    )
    async def bloxlink_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not os.getenv("BLOXLINK_API_KEY"):
            await interaction.followup.send(
                "❌ Bloxlink is not configured on this server.", ephemeral=True
            )
            return

        roblox_id, roblox_username = await _try_bloxlink(
            interaction.user.id, interaction.guild_id
        )

        if not roblox_id:
            await interaction.followup.send(
                "❌ Bloxlink doesn't have a record for your Discord account.\n"
                "Visit https://blox.link and verify there first, then run this command again.\n"
                "Or use `/verify roblox_username:YourName` for manual verification.",
                ephemeral=True,
            )
            return

        existing = await db.get_roblox_username(interaction.user.id)
        if existing and existing.lower() == roblox_username.lower():
            await interaction.followup.send(
                f"✅ Already verified as **{existing}** — nothing to update.",
                ephemeral=True,
            )
            return

        if await db.is_roblox_verified(roblox_username):
            await interaction.followup.send(
                f"❌ Roblox account **{roblox_username}** is already linked to another Discord account.",
                ephemeral=True,
            )
            return

        # Remove old verification if they had one
        if existing:
            await db.remove_verification(interaction.user.id)

        success = await _complete_verification(
            interaction.user, interaction.guild, interaction.user.id, roblox_username
        )
        if success:
            msg = (
                f"✅ Updated verification to **{roblox_username}** via Bloxlink!"
                if existing
                else f"✅ Verified as **{roblox_username}** via Bloxlink! Your nickname has been updated."
            )
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(
                "❌ Verification failed. The Roblox account may be linked to another user.",
                ephemeral=True,
            )

    # ── /whois ────────────────────────────────────────────────────────────────

    @app_commands.command(name="whois", description="Look up the Roblox account linked to a Discord member.")
    @app_commands.describe(member="The Discord member to look up (default: yourself)")
    async def whois(self, interaction: discord.Interaction, member: discord.Member = None):
        target      = member or interaction.user
        roblox_name = await db.get_roblox_username(target.id)

        if not roblox_name:
            await interaction.response.send_message(
                f"{target.mention} hasn't verified their Roblox account yet.",
                ephemeral=True,
            )
            return

        roblox_id = await get_user_id_by_name(roblox_name)
        embed = discord.Embed(title="🔎 Roblox Lookup", color=discord.Color.green())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Discord",         value=target.mention, inline=True)
        embed.add_field(name="Roblox Username", value=roblox_name,    inline=True)
        if roblox_id:
            embed.add_field(
                name="Profile",
                value=f"[View](https://www.roblox.com/users/{roblox_id}/profile)",
                inline=True,
            )
        await interaction.response.send_message(embed=embed)

    # ── /verify-all ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="verify-all",
        description="(Owner only) Bulk-verify all unverified members via Bloxlink.",
    )
    async def verify_all(self, interaction: discord.Interaction):
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Only the bot owner can run this command.", ephemeral=True
            )
            return

        if not os.getenv("BLOXLINK_API_KEY"):
            await interaction.response.send_message(
                "❌ Bloxlink is not configured (`BLOXLINK_API_KEY` missing).", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        success_count  = 0
        skip_verified  = 0
        skip_conflict  = 0
        skip_not_found = 0
        errors         = 0

        members = [m for m in interaction.guild.members if not m.bot]

        for member in members:
            # Already verified in our DB?
            existing = await db.get_roblox_username(member.id)
            if existing:
                # Make sure they have the role
                await _assign_verified_role(member, interaction.guild)
                skip_verified += 1
                continue

            roblox_id, roblox_username = await _try_bloxlink(member.id, interaction.guild_id)
            if not roblox_id:
                skip_not_found += 1
                continue

            if await db.is_roblox_verified(roblox_username):
                skip_conflict += 1
                continue

            try:
                ok = await _complete_verification(
                    member, interaction.guild, member.id, roblox_username
                )
                if ok:
                    success_count += 1
                    log.info(f"verify-all: auto-verified {member} as {roblox_username}")
                    try:
                        embed = discord.Embed(
                            title="✅ Automatically Verified!",
                            description=(
                                f"You've been verified in **{interaction.guild.name}** "
                                f"as **{roblox_username}** via Bloxlink.\n"
                                "Your nickname has been updated."
                            ),
                            color=discord.Color.green(),
                        )
                        await member.send(embed=embed)
                    except discord.Forbidden:
                        pass
                else:
                    skip_conflict += 1
            except Exception as e:
                log.warning(f"verify-all: error verifying {member}: {e}")
                errors += 1

        lines = [
            f"✅ **{success_count}** newly verified via Bloxlink",
            f"🔁 **{skip_verified}** already verified (roles refreshed)",
            f"🔍 **{skip_not_found}** not found in Bloxlink",
            f"⚠️ **{skip_conflict}** skipped (Roblox account already claimed)",
        ]
        if errors:
            lines.append(f"❌ **{errors}** errors (check logs)")

        embed = discord.Embed(
            title="🔗 Verify-All Complete",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Checked {len(members)} members")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /reverify ─────────────────────────────────────────────────────────────

    @app_commands.command(name="reverify", description="(Staff) Reset a member's Roblox verification.")
    @app_commands.describe(member="Member to reset")
    @app_commands.default_permissions(manage_roles=True)
    async def reverify(self, interaction: discord.Interaction, member: discord.Member):
        old_name = await db.get_roblox_username(member.id)
        await db.remove_verification(member.id)

        if VERIFIED_ROLE_ID:
            role = interaction.guild.get_role(VERIFIED_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    pass

        suffix = f" (was **{old_name}**)" if old_name else ""
        await interaction.response.send_message(
            f"✅ {member.mention}'s Roblox verification has been reset{suffix}. "
            "They can now run `/verify` again.",
            ephemeral=True,
        )


# ── Manual verification confirm view ─────────────────────────────────────────

class VerifyConfirmView(discord.ui.View):
    def __init__(self, discord_id, roblox_id, roblox_username, code, bot):
        super().__init__(timeout=CODE_EXPIRE_MINUTES * 60)
        self.discord_id      = discord_id
        self.roblox_id       = roblox_id
        self.roblox_username = roblox_username
        self.code            = code
        self.bot             = bot

    async def on_timeout(self):
        # Clean up pending verification on expiry
        await db.store_pending_verification.__wrapped__ if hasattr(
            db.store_pending_verification, "__wrapped__"
        ) else None

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification session.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        pending = await db.get_pending_verification(self.discord_id)
        if not pending:
            await interaction.followup.send(
                "❌ Verification session expired. Run `/verify` again.", ephemeral=True
            )
            return

        expires = datetime.fromisoformat(pending["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            await interaction.followup.send(
                "❌ Code expired. Run `/verify` again.", ephemeral=True
            )
            return

        description = await get_profile_description(self.roblox_id)
        if description is None:
            await interaction.followup.send(
                "❌ Couldn't fetch your Roblox profile. Try again in a moment.", ephemeral=True
            )
            return

        if pending["code"] not in description:
            await interaction.followup.send(
                f"❌ Code not found in your Roblox About section.\n"
                f"Make sure you saved the profile after pasting:\n"
                f"```\n{pending['code']}\n```",
                ephemeral=True,
            )
            return

        success = await _complete_verification(
            interaction.user, interaction.guild, self.discord_id, self.roblox_username
        )

        for child in self.children:
            child.disabled = True
        self.stop()

        if success:
            await interaction.followup.send(
                f"✅ Successfully verified as **{self.roblox_username}**! "
                "Your nickname has been updated.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"❌ Roblox account **{self.roblox_username}** is already linked to another Discord account.",
                ephemeral=True,
            )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification session.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Verification cancelled.", embed=None, view=None)


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))