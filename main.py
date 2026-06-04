"""
Main entry point for the Roblox Group Discord Bot.
"""
import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv
from utils.roblox_api import close_session
from logging.handlers import RotatingFileHandler
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            "bot.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8"
        )
    ],
)
log = logging.getLogger("bot")

COGS = [
    "cogs.verify",
    "cogs.role_binds",
    "cogs.roblox_group",
    "cogs.ep",
    "cogs.log_event",
    "cogs.promotions",
    "cogs.moderation",
    "cogs.report",
    "cogs.profile",
]

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True


class GroupBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        failed = []
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.error(f"Failed to load cog {cog}: {e}", exc_info=True)
                failed.append(cog)

        if failed:
            log.warning(f"Failed to load {len(failed)} cog(s): {failed}")

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Synced slash commands to guild {guild_id}")
        else:
            await self.tree.sync()
            log.info("Synced slash commands globally")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Serving {len(self.guilds)} guild(s)")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="",
            )
        )

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ):
        """Global slash-command error handler — catches anything not handled in the cog."""
        if isinstance(error, discord.app_commands.MissingPermissions):
            msg = "❌ You don't have permission to use that command."
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            msg = f"⏳ This command is on cooldown. Try again in {error.retry_after:.1f}s."
        elif isinstance(error, discord.app_commands.BotMissingPermissions):
            msg = f"❌ I'm missing permissions to do that: `{error.missing_permissions}`"
        else:
            log.error(f"Unhandled app command error in /{interaction.command}: {error}", exc_info=True)
            msg = "❌ Something went wrong. Please try again."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        log.error(f"Unhandled prefix command error: {error}", exc_info=True)


bot = GroupBot()


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.critical("DISCORD_TOKEN not set in .env — cannot start.")
        sys.exit(1)

    if not os.getenv("ROBLOX_GROUP_ID"):
        log.warning("ROBLOX_GROUP_ID not set — Roblox group commands will not work.")

    async with bot:
        try:
            await bot.start(token)
        finally:
            await close_session()


if __name__ == "__main__":
    asyncio.run(main())