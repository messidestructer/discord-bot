"""
Roblox verification cog.
Default flow: Bloxlink lookup → instant verification if found.
Fallback:     Manual code placement in the Roblox profile About section.
"""
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

VERIFIED_ROLE_ID     = int(os.getenv("VERIFIED_ROLE_ID", "0"))
CODE_EXPIRE_MINUTES  = 15


def _make_code() -> str:
    words = [
        "amber", "bravo", "cedar", "delta", "eagle", "falcon", "gamma",
        "hotel", "indigo", "jade", "kilo", "lemon", "maple", "nova",
        "ocean", "pearl", "quest", "rapid", "solar", "tiger", "ultra",
        "valor", "wave", "xenon", "yield", "zeal",
    ]
    return "-".join(random.choices(words, k=4))


async def _complete_verification(
    interaction_or_member,   # discord.Interaction or discord.Member
    guild: discord.Guild,
    discord_id: int,
    roblox_username: str,
) -> None:
    """Persist the verification, set nickname, and assign the verified role."""
    success = await db.claim_verification(
    discord_id,
    roblox_username,
)

    if not success:
        return

    member = (
        interaction_or_member
        if isinstance(interaction_or_member, discord.Member)
        else guild.get_member(discord_id)
    )
    if not member:
        return

    try:
        await member.edit(nick=roblox_username)
    except discord.Forbidden:
        pass

    if VERIFIED_ROLE_ID:
        role = guild.get_role(VERIFIED_ROLE_ID)
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ── /verify ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="verify",
        description="Link your Roblox account to Discord. Uses Bloxlink automatically if available.",
    )
    @app_commands.describe(roblox_username="Your Roblox username (only needed if Bloxlink can't find you)")
    async def verify(self, interaction: discord.Interaction, roblox_username: str = None):
        await interaction.response.defer(ephemeral=True)

        # Already verified?
        existing = await db.get_roblox_username(interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"✅ {interaction.user.mention} you're already verified as **{existing}**.\n"
                "Need to re-verify? Contact a staff member.",
                ephemeral=True,
            )
            return

        # ── Try Bloxlink first ────────────────────────────────────────────────
        bloxlink_key = os.getenv("BLOXLINK_API_KEY")
        if bloxlink_key:
            roblox_id = await bloxlink_get_roblox_id(
                interaction.user.id, str(interaction.guild_id)
            )
            if roblox_id:
                profile       = await get_user_profile(roblox_id)
                canonical     = profile["name"] if profile else str(roblox_id)
                await _complete_verification(interaction.user, interaction.guild, interaction.user.id, canonical)
                await interaction.followup.send(
                    f"✅ {interaction.user.mention} verified via **Bloxlink** as **{canonical}**!\n"
                    f"Your nickname has been updated.",
                    ephemeral=True,
                )
                return
            # Bloxlink found nothing — fall through to manual
            notice = (
                "ℹ️ Bloxlink couldn't find your account automatically. "
                "Falling back to manual verification.\n\n"
            )
        else:
            notice = ""

        # ── Manual fallback — require username ────────────────────────────────
        if not roblox_username:
            await interaction.followup.send(
                f"{notice}❌ Please provide your Roblox username: `/verify roblox_username:<name>`",
                ephemeral=True,
            )
            return

        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(
                f"❌ Could not find Roblox user **{roblox_username}**. "
                "Check the spelling and try again.",
                ephemeral=True,
            )
            return

        profile       = await get_user_profile(roblox_id)
        canonical     = profile.get("name", roblox_username) if profile else roblox_username
        if await db.is_roblox_verified(canonical):
            await interaction.followup.send(
                f"❌ Roblox account **{canonical}** is already linked to another Discord account.",
                ephemeral=True,
            )
            return
        code          = _make_code()
        expires_at    = (datetime.now(timezone.utc) + timedelta(minutes=CODE_EXPIRE_MINUTES)).isoformat()
        await db.store_pending_verification(interaction.user.id, canonical, code, expires_at)

        embed = discord.Embed(
            title="🔗 Roblox Verification",
            color=discord.Color.blue(),
            description=(
                f"{interaction.user.mention}, to verify as **{canonical}** paste the code below "
                f"into the **About** section of your "
                f"[Roblox profile](https://www.roblox.com/users/{roblox_id}/profile).\n\n"
                f"```\n{code}\n```\n"
                f"Then click **Confirm** below.\n"
                f"Code expires in **{CODE_EXPIRE_MINUTES} minutes**."
            ),
        )
        embed.set_footer(text=f"Roblox ID: {roblox_id}")

        view = VerifyConfirmView(interaction.user.id, roblox_id, canonical, code, self.bot)
        await interaction.followup.send(f"{notice}", embed=embed, view=view, ephemeral=True)

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
        embed.add_field(name="Discord",          value=target.mention,  inline=True)
        embed.add_field(name="Roblox Username",  value=roblox_name,     inline=True)
        if roblox_id:
            embed.add_field(
                name="Profile",
                value=f"[View](https://www.roblox.com/users/{roblox_id}/profile)",
                inline=True,
            )
        await interaction.response.send_message(embed=embed)

    # ── /reverify ─────────────────────────────────────────────────────────────

    @app_commands.command(name="reverify", description="(Staff) Unlink and reset a member's Roblox verification.")
    @app_commands.describe(member="Member to reset")
    @app_commands.default_permissions(manage_roles=True)
    async def reverify(self, interaction: discord.Interaction, member: discord.Member):
        await db.remove_verification(member.id)
        # Remove verified role if present
        if VERIFIED_ROLE_ID:
            role = interaction.guild.get_role(VERIFIED_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    pass
        await interaction.response.send_message(
            f"✅ {member.mention}'s Roblox verification has been reset. "
            "They can now run `/verify` again.",
            ephemeral=True,
        )


# ── Manual verification view ──────────────────────────────────────────────────

class VerifyConfirmView(discord.ui.View):
    def __init__(self, discord_id, roblox_id, roblox_username, code, bot):
        super().__init__(timeout=CODE_EXPIRE_MINUTES * 60)
        self.discord_id      = discord_id
        self.roblox_id       = roblox_id
        self.roblox_username = roblox_username
        self.code            = code
        self.bot             = bot

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification session.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        pending = await db.get_pending_verification(self.discord_id)
        if not pending:
            await interaction.followup.send("❌ Verification session expired. Run `/verify` again.", ephemeral=True)
            return

        expires = datetime.fromisoformat(pending["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            await interaction.followup.send("❌ Code expired. Run `/verify` again.", ephemeral=True)
            return

        description = await get_profile_description(self.roblox_id)
        if description is None:
            await interaction.followup.send("❌ Couldn't fetch your Roblox profile. Try again.", ephemeral=True)
            return

        if pending["code"] not in description:
            await interaction.followup.send(
                f"❌ Code not found in your Roblox About section. Make sure you saved it:\n"
                f"```\n{pending['code']}\n```",
                ephemeral=True,
            )
            return

        await _complete_verification(
            interaction.user, interaction.guild, self.discord_id, self.roblox_username
        )
        self.stop()
        await interaction.followup.send(
            f"✅ {interaction.user.mention} successfully verified as **{self.roblox_username}**! "
            "Your nickname has been updated.",
            ephemeral=True,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification session.", ephemeral=True)
            return
        self.stop()
        await interaction.response.send_message("Verification cancelled.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))