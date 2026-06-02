"""
Roblox verification cog.
Members verify with /verify — the bot checks their Roblox profile for a short code.
Also supports Bloxlink if an API key is configured.
"""
import os
import random
import string
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

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))
CODE_EXPIRE_MINUTES = 15


def _make_code() -> str:
    words = [
        "amber", "bravo", "cedar", "delta", "eagle", "falcon", "gamma",
        "hotel", "indigo", "jade", "kilo", "lemon", "maple", "nova",
        "ocean", "pearl", "quest", "rapid", "solar", "tiger", "ultra",
        "valor", "wave", "xenon", "yield", "zeal",
    ]
    return "-".join(random.choices(words, k=4))


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="verify", description="Link your Roblox account to Discord.")
    @app_commands.describe(roblox_username="Your Roblox username")
    async def verify(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer(ephemeral=True)

        # Check if already verified
        existing = await db.get_roblox_username(interaction.user.id)
        if existing:
            await interaction.followup.send(
                f"✅ You're already verified as **{existing}**.\n"
                "If you need to re-verify, contact a staff member.",
                ephemeral=True,
            )
            return

        # Validate username exists on Roblox
        roblox_id = await get_user_id_by_name(roblox_username)
        if not roblox_id:
            await interaction.followup.send(
                f"❌ Could not find Roblox user **{roblox_username}**. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        # Fetch canonical username from Roblox
        profile = await get_user_profile(roblox_id)
        canonical_name = profile.get("name", roblox_username) if profile else roblox_username

        # Generate code
        code = _make_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=CODE_EXPIRE_MINUTES)).isoformat()
        await db.store_pending_verification(interaction.user.id, canonical_name, code, expires_at)

        embed = discord.Embed(
            title="🔗 Roblox Verification",
            color=discord.Color.blue(),
            description=(
                f"To verify as **{canonical_name}**, paste the code below into the "
                f"**About** section of your [Roblox profile](https://www.roblox.com/users/{roblox_id}/profile).\n\n"
                f"```\n{code}\n```\n"
                f"Then click **Confirm** below. Code expires in **{CODE_EXPIRE_MINUTES} minutes**."
            ),
        )
        embed.set_footer(text=f"User ID: {roblox_id}")

        view = VerifyConfirmView(interaction.user.id, roblox_id, canonical_name, code, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="whois", description="Look up the Roblox account linked to a Discord user.")
    @app_commands.describe(member="The Discord member to look up")
    async def whois(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        roblox_name = await db.get_roblox_username(target.id)
        if not roblox_name:
            await interaction.response.send_message(
                f"**{target.display_name}** has not verified their Roblox account.",
                ephemeral=True,
            )
            return
        roblox_id = await get_user_id_by_name(roblox_name)
        embed = discord.Embed(
            title=f"🔎 {target.display_name}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Roblox Username", value=roblox_name, inline=True)
        if roblox_id:
            embed.add_field(
                name="Profile",
                value=f"[View Profile](https://www.roblox.com/users/{roblox_id}/profile)",
                inline=True,
            )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="bloxlink-sync", description="Auto-verify using your Bloxlink account.")
    async def bloxlink_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bloxlink_key = os.getenv("BLOXLINK_API_KEY")
        if not bloxlink_key:
            await interaction.followup.send(
                "❌ Bloxlink is not configured on this bot. Use `/verify` instead.",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        roblox_id = await bloxlink_get_roblox_id(interaction.user.id, guild_id)
        if not roblox_id:
            await interaction.followup.send(
                "❌ Could not find your Roblox account via Bloxlink. Make sure you've verified with Bloxlink first (`!verify` in a server with the Bloxlink bot).",
                ephemeral=True,
            )
            return

        profile = await get_user_profile(roblox_id)
        if not profile:
            await interaction.followup.send("❌ Could not fetch your Roblox profile.", ephemeral=True)
            return

        canonical_name = profile["name"]
        await db.confirm_verification(interaction.user.id, canonical_name)

        # Set nickname
        try:
            await interaction.user.edit(nick=canonical_name)
        except discord.Forbidden:
            pass

        # Assign verified role
        if VERIFIED_ROLE_ID:
            role = interaction.guild.get_role(VERIFIED_ROLE_ID)
            if role:
                try:
                    await interaction.user.add_roles(role)
                except discord.Forbidden:
                    pass

        await interaction.followup.send(
            f"✅ Verified via Bloxlink as **{canonical_name}**!", ephemeral=True
        )


class VerifyConfirmView(discord.ui.View):
    def __init__(self, discord_id, roblox_id, roblox_username, code, bot):
        super().__init__(timeout=CODE_EXPIRE_MINUTES * 60)
        self.discord_id = discord_id
        self.roblox_id = roblox_id
        self.roblox_username = roblox_username
        self.code = code
        self.bot = bot

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Check pending still valid
        pending = await db.get_pending_verification(self.discord_id)
        if not pending:
            await interaction.followup.send("❌ Verification expired. Run `/verify` again.", ephemeral=True)
            return

        expires = datetime.fromisoformat(pending["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires:
            await interaction.followup.send("❌ Code expired. Run `/verify` again.", ephemeral=True)
            return

        # Fetch Roblox profile and check description
        description = await get_profile_description(self.roblox_id)
        if description is None:
            await interaction.followup.send("❌ Could not fetch your Roblox profile.", ephemeral=True)
            return

        if self.code not in description:
            await interaction.followup.send(
                f"❌ Code not found in your Roblox About section. Make sure you saved it:\n```\n{self.code}\n```",
                ephemeral=True,
            )
            return

        # Success
        await db.confirm_verification(self.discord_id, self.roblox_username)

        # Set nickname
        guild = interaction.guild
        member = guild.get_member(self.discord_id)
        if member:
            try:
                await member.edit(nick=self.roblox_username)
            except discord.Forbidden:
                pass
            if VERIFIED_ROLE_ID:
                role = guild.get_role(VERIFIED_ROLE_ID)
                if role:
                    try:
                        await member.add_roles(role)
                    except discord.Forbidden:
                        pass

        self.stop()
        await interaction.followup.send(
            f"✅ Successfully verified as **{self.roblox_username}**! Your nickname has been updated.",
            ephemeral=True,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message("This isn't your verification.", ephemeral=True)
            return
        self.stop()
        await interaction.response.send_message("Verification cancelled.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))
