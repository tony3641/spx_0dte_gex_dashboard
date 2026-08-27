"""
Runtime owner of the Discord subsystem: current settings, the live bot, and
try-then-commit applies (a new bot must log in before the old one is replaced;
.env is written only on success).

config module globals are never mutated at runtime — bots are constructed with
explicit settings. The manager's run task is deliberately NOT in
state.background_tasks, so IB reconnects (which cancel all background tasks)
cannot kill the bot.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import discord

import config
from discord_bot import make_discord_bot
from env_store import update_env

logger = logging.getLogger(__name__)

READY_TIMEOUT_S = 15.0

DISCORD_ENV_KEYS = ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_CHANNEL_ID",
                    "DISCORD_ALLOWED_USER_IDS", "DISCORD_ALLOWED_ROLE")


@dataclass
class DiscordSettings:
    token: str = ""
    guild_id: str = ""
    channel_id: str = ""
    allowed_user_ids: list = field(default_factory=list)
    allowed_role: str = ""


def load_initial_settings() -> DiscordSettings:
    """Fold already-resolved config values (env var / .env / yaml) into settings."""
    return DiscordSettings(
        token=config.DISCORD_TOKEN or "",
        guild_id=str(config.DISCORD_GUILD_ID or ""),
        channel_id=str(config.DISCORD_CHANNEL_ID or ""),
        allowed_user_ids=list(config.DISCORD_ALLOWED_USER_IDS or []),
        allowed_role=config.DISCORD_ALLOWED_ROLE or "",
    )


def _friendly_error(e: BaseException) -> str:
    if isinstance(e, discord.LoginFailure):
        return "Discord rejected the token."
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return (f"Discord did not become ready within {READY_TIMEOUT_S:.0f}s "
                f"(check network/intents).")
    return f"{type(e).__name__}: {e}"


class DiscordSettingsManager:
    def __init__(self, ib, state, bot_factory=make_discord_bot,
                 persist: Callable = update_env):
        self.ib = ib
        self.state = state
        self._bot_factory = bot_factory
        self._persist = persist
        self.settings: DiscordSettings = load_initial_settings()
        self.bot = None
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.bot is not None

    async def apply(self, new: DiscordSettings) -> dict:
        # Disabled (no token): stop anything running, adopt settings, persist.
        if not new.token:
            await self._stop_bot()
            self.settings = new
            return {"ok": True, "running": False,
                    "persisted": self._persist_discord_env(new)}

        bot = self._bot_factory(
            self.ib, self.state,
            token=new.token,
            guild_id=new.guild_id or None,
            channel_id=new.channel_id,
            allowed_user_ids=new.allowed_user_ids,
            allowed_role=new.allowed_role,
        )
        task = asyncio.create_task(bot.start())
        waiter = asyncio.create_task(bot.bot.wait_until_ready())
        done, _ = await asyncio.wait({task, waiter}, timeout=READY_TIMEOUT_S,
                                     return_when=asyncio.FIRST_COMPLETED)

        err: Optional[BaseException] = None
        if task in done:
            if task.cancelled():
                err = RuntimeError("Discord bot task was cancelled during startup")
            else:
                err = task.exception() or RuntimeError(
                    "Discord bot task exited before becoming ready")
        elif waiter not in done:
            err = TimeoutError(f"not ready within {READY_TIMEOUT_S:.0f}s")

        if err is not None:
            waiter.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            try:
                await bot.close()
            except Exception:
                pass
            return {"ok": False, "error": _friendly_error(err)}

        # Ready: swap over, then close the old bot.
        old_bot, old_task = self.bot, self._task
        self.bot = bot
        self._task = task
        self.state.alert_bridge = bot.alert_bridge
        self.settings = new
        if old_bot is not None:
            await self._close_bot(old_bot, old_task)

        return {"ok": True, "running": True,
                "persisted": self._persist_discord_env(new)}

    async def stop(self):
        """Stop the bot (idempotent). Used on shutdown and when disabled."""
        await self._stop_bot()

    async def _stop_bot(self):
        if self.bot is not None:
            await self._close_bot(self.bot, self._task)
        self.bot = None
        self._task = None
        self.state.alert_bridge = None

    async def _close_bot(self, bot, task: Optional[asyncio.Task]):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            await bot.close()
        except Exception:
            pass

    def _persist_discord_env(self, s: DiscordSettings) -> bool:
        try:
            self._persist({
                "DISCORD_TOKEN": s.token,
                "DISCORD_GUILD_ID": s.guild_id,
                "DISCORD_CHANNEL_ID": s.channel_id,
                "DISCORD_ALLOWED_USER_IDS": ",".join(str(u) for u in s.allowed_user_ids),
                "DISCORD_ALLOWED_ROLE": s.allowed_role,
            })
            return True
        except Exception as e:
            logger.warning(f"Failed to persist Discord settings to .env: {e}")
            return False
