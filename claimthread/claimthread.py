"""
claimthread — a Modmail plugin.

Adds `?claim` / `?unclaim` to Modmail threads:

  * `?claim`   assigns the thread to you, renames the channel to put your
               name on it, and pings you (with the `:Jet2Message:` custom
               emoji) whenever the recipient sends a new message in this
               thread from then on — the mention is added directly onto
               Modmail's own relay message, not sent as a separate message.
  * `?unclaim` releases the claim and restores the original channel name.

The claim is stored in the bot's own database (via `bot.plugin_db`), so it
survives a bot restart. Nothing here touches core Modmail files — this is
a self-contained plugin, per https://docs.modmail.dev/usage-guide/plugins.

Install (after pushing this folder to your own GitHub repo):
    ?plugin add <your-github-username>/<your-repo>/claimthread[@branch]

Adjust who can use the commands with Modmail's normal permission system,
e.g.:
    ?permissions add level supporter claim
    ?permissions add level supporter unclaim
"""

import re

import discord
from discord.ext import commands

from core import checks
from core.models import PermissionLevel, getLogger

logger = getLogger(__name__)


class ClaimThread(commands.Cog):
    """Claim a thread: renames the channel and pings you on new messages."""

    def __init__(self, bot):
        self.bot = bot
        # Async Mongo collection scoped to this cog — persists across restarts.
        self.db = bot.plugin_db.get_partition(self)

    # ---------------------------------------------------------------
    # storage helpers
    # ---------------------------------------------------------------

    async def _get_claim(self, channel_id: int):
        return await self.db.find_one({"channel_id": str(channel_id)})

    async def _set_claim(self, channel_id: int, user_id: int, base_name: str):
        await self.db.find_one_and_update(
            {"channel_id": str(channel_id)},
            {
                "$set": {
                    "channel_id": str(channel_id),
                    "user_id": str(user_id),
                    "base_name": base_name,
                }
            },
            upsert=True,
        )

    async def _clear_claim(self, channel_id: int):
        await self.db.delete_one({"channel_id": str(channel_id)})

    @staticmethod
    def _slug(name: str) -> str:
        """Turn a display name into something Discord accepts in a channel name."""
        name = name.lower().strip()
        name = re.sub(r"[^a-z0-9\-]+", "-", name)
        name = re.sub(r"-+", "-", name).strip("-")
        return name or "staff"

    # ---------------------------------------------------------------
    # commands
    # ---------------------------------------------------------------

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def claim(self, ctx):
        """Claim this thread and get pinged on the recipient's next messages."""
        thread = ctx.thread
        channel = thread.channel

        existing = await self._get_claim(channel.id)
        is_admin = ctx.channel.permissions_for(ctx.author).administrator

        if existing and int(existing["user_id"]) != ctx.author.id and not is_admin:
            embed = discord.Embed(
                description=(
                    f"This thread is already claimed by <@{existing['user_id']}>. "
                    f"They (or an admin) need to `{ctx.prefix}unclaim` it first."
                ),
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        # Keep the pre-claim name around so unclaim can restore it, even if
        # someone re-claims an already-claimed thread as an admin override.
        base_name = existing["base_name"] if existing else channel.name
        await self._set_claim(channel.id, ctx.author.id, base_name)

        new_name = f"{self._slug(ctx.author.display_name)}-{base_name}"[:100]
        try:
            await channel.edit(name=new_name, reason=f"Claimed by {ctx.author}.")
        except discord.HTTPException as e:
            logger.warning("claimthread: failed to rename channel %s: %s", channel.id, e)

        embed = discord.Embed(
            description=(
                f"{ctx.author.mention} claimed this thread. "
                f"They'll be pinged here whenever {thread.recipient} sends a new message."
            ),
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @commands.command(aliases=["removeclaim", "remove-claim"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unclaim(self, ctx):
        """Release your claim on this thread."""
        thread = ctx.thread
        channel = thread.channel

        existing = await self._get_claim(channel.id)
        if not existing:
            embed = discord.Embed(description="This thread isn't claimed.", color=self.bot.error_color)
            return await ctx.send(embed=embed)

        is_claimer = int(existing["user_id"]) == ctx.author.id
        is_admin = ctx.channel.permissions_for(ctx.author).administrator
        if not (is_claimer or is_admin):
            embed = discord.Embed(
                description=f"Only <@{existing['user_id']}> (or an admin) can unclaim this thread.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        await self._clear_claim(channel.id)
        try:
            await channel.edit(name=existing["base_name"][:100], reason=f"Unclaimed by {ctx.author}.")
        except discord.HTTPException as e:
            logger.warning("claimthread: failed to restore channel name %s: %s", channel.id, e)

        embed = discord.Embed(
            description=f"{ctx.author.mention} released the claim on this thread.",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------
    # listeners
    # ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        """Ping the claimer whenever the recipient sends a new message.

        This edits the mention straight into the message Modmail itself just
        posted for the recipient's message, instead of sending a separate
        message underneath it.
        """
        if from_mod:
            return  # only the recipient's own messages should ping the claimer

        channel = thread.channel
        if channel is None:
            return

        existing = await self._get_claim(channel.id)
        if not existing:
            return

        guild = self.bot.guild
        emoji = discord.utils.get(guild.emojis, name="Jet2Message") if guild else None
        emoji_str = str(emoji) if emoji else "\N{BELL}"
        mention = f"<@{existing['user_id']}> {emoji_str}"

        incoming_text = getattr(message, "content", "") or ""

        # Find the message Modmail just relayed for this reply (posted by the
        # bot itself, as an embed with no content yet) and add our mention to
        # it directly, rather than sending a new message after it.
        target = None
        async for msg in channel.history(limit=5):
            if not (msg.author.id == self.bot.user.id and msg.embeds and not msg.content):
                continue
            description = msg.embeds[0].description or ""
            if incoming_text and incoming_text not in description:
                continue
            target = msg
            break

        if target is not None:
            try:
                await target.edit(content=mention)
                return
            except discord.HTTPException as e:
                logger.warning("claimthread: failed to edit relay message in %s: %s", channel.id, e)

        # Fallback (couldn't locate the relay message): send the ping on its own
        # rather than silently dropping it.
        try:
            await channel.send(mention)
        except discord.HTTPException as e:
            logger.warning("claimthread: failed to send ping in %s: %s", channel.id, e)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        """Clean up the claim record once a thread closes."""
        if thread.channel is not None:
            await self._clear_claim(thread.channel.id)


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
