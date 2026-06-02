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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("bot")

COGS = [
    "cogs.verify",
    "cogs.roblox_group",
    "cogs.ep",
    "cogs.log_event",
    "cogs.promotions",
    "cogs.moderation",
    "cogs.report",
    "cogs.profile",
    "cogs.fun",
]

intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class GroupBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.error(f"Failed to load cog {cog}: {e}", exc_info=True)

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
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the Roblox group"
            )
        )

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command.", ephemeral=True)
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            log.error(f"Unhandled command error: {error}", exc_info=True)


bot = GroupBot()


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.critical("DISCORD_TOKEN not set in .env — bot cannot start.")
        sys.exit(1)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
