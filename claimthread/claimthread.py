"""
claimthread — a Modmail plugin.

Adds `?claim` / `?unclaim` to Modmail threads:

  * `?claim`   assigns the thread to you, renames the channel to put your
               name on it, and makes sure you (with the `:Jet2Message:`
               custom emoji next to your mention) get pinged on the same
               message every time the recipient sends a new message in
               this thread from then on.
  * `?unclaim` releases the claim, restores the original channel name, and
               stops the pings.

Rather than sending a second message underneath Modmail's own relay
message (which showed the mention but did NOT actually trigger a Discord
notification — editing a message to add a mention never pings, only
mentions present when a message is first sent do), this hooks into
Modmail core's own built-in "subscriptions" mechanism (the same one
`?subscribe` uses). That mention is included in the *same* send call
Modmail uses to post the relayed message, so it's a real, guaranteed
notification, not just a highlighted mention.

The claim itself (who claimed which channel, and what to restore on
unclaim) is stored in the bot's own database via `bot.plugin_db`, so it
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
    # claim storage helpers (channel_id -> who claimed it, and what to
    # restore / remove on unclaim)
    # ---------------------------------------------------------------

    async def _get_claim(self, channel_id: int):
        return await self.db.find_one({"channel_id": str(channel_id)})

    async def _set_claim(self, channel_id: int, user_id: int, base_name: str, sub_entry: str, thread_key: str):
        await self.db.find_one_and_update(
            {"channel_id": str(channel_id)},
            {
                "$set": {
                    "channel_id": str(channel_id),
                    "user_id": str(user_id),
                    "base_name": base_name,
                    "sub_entry": sub_entry,
                    "thread_key": thread_key,
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

    def _mention_emoji(self) -> str:
        guild = self.bot.guild
        emoji = discord.utils.get(guild.emojis, name="Jet2Message") if guild else None
        return str(emoji) if emoji else "\N{BELL}"

    # ---------------------------------------------------------------
    # Modmail's own per-thread "subscriptions" list (same one ?subscribe
    # uses) — entries here get pinged, as real content, on every relayed
    # message for that thread from now on.
    # ---------------------------------------------------------------

    async def _add_subscription(self, thread_key: str, entry: str):
        if thread_key not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][thread_key] = []
        subs = self.bot.config["subscriptions"][thread_key]
        if entry not in subs:
            subs.append(entry)
            await self.bot.config.update()

    async def _remove_subscription(self, thread_key: str, entry: str):
        subs = self.bot.config["subscriptions"].get(thread_key, [])
        if entry in subs:
            subs.remove(entry)
            await self.bot.config.update()

    # ---------------------------------------------------------------
    # commands
    # ---------------------------------------------------------------

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def claim(self, ctx):
        """Claim this thread and get pinged (for real) on the recipient's next messages."""
        thread = ctx.thread
        channel = thread.channel
        thread_key = str(thread.id)

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

        if existing and int(existing["user_id"]) != ctx.author.id:
            # Admin override: drop the previous claimer's ping before adding ours.
            await self._remove_subscription(
                existing.get("thread_key", thread_key),
                existing.get("sub_entry", f"<@{existing['user_id']}>"),
            )

        # Keep the pre-claim name around so unclaim can restore it, even if
        # someone re-claims an already-claimed thread as an admin override.
        base_name = existing["base_name"] if existing else channel.name
        sub_entry = f"<@{ctx.author.id}> {self._mention_emoji()}"

        await self._set_claim(channel.id, ctx.author.id, base_name, sub_entry, thread_key)
        await self._add_subscription(thread_key, sub_entry)

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

        await self._remove_subscription(
            existing.get("thread_key", str(thread.id)),
            existing.get("sub_entry", f"<@{existing['user_id']}>"),
        )
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
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        """Clean up the claim record once a thread closes.

        (Modmail core already wipes the thread's "subscriptions" entry on
        close, so we only need to clean up our own claim record here.)
        """
        if thread.channel is not None:
            await self._clear_claim(thread.channel.id)


async def setup(bot):
    await bot.add_cog(ClaimThread(bot))
