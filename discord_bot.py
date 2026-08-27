"""
Discord bridge: command logic + event alerting.

Pure, engine-facing functions (the unit-test surface) are separated from the
discord.py glue so they can be tested without a live gateway.
"""
import logging
from typing import List, Optional, Set

import config
from account_manager import build_account_payload
from market_hours import market_status, get_expiration_display
from strategy_engine import (build_candidate_views, reset_strategy_runtime)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def is_authorized(user_id: int, allowed_user_ids: list, allowed_role: str,
                  role_names, role_ids) -> bool:
    """True when the user is in the ID allowlist or has a matching role."""
    if user_id in (allowed_user_ids or []):
        return True
    if allowed_role:
        role = str(allowed_role)
        names = {str(n).lower() for n in (role_names or [])}
        ids = {str(r) for r in (role_ids or [])}
        if role.lower() in names or role in ids:
            return True
    return False


# ---------------------------------------------------------------------------
# Query views (read current state; no order actions)
# ---------------------------------------------------------------------------
def account_view(state) -> dict:
    return build_account_payload(state)


def positions_view(state, filter=None) -> dict:
    pos = list(getattr(state, "positions", []) or [])
    if filter:
        f = str(filter).lower()
        pos = [p for p in pos if f in (p.get("contract") or {}).get("symbol", "").lower()
               or f in (p.get("contract") or {}).get("localSymbol", "").lower()]
    return {"count": len(pos), "positions": pos}


def orders_view(state) -> dict:
    orders = list(getattr(state, "open_orders", []) or [])
    return {"count": len(orders), "orders": orders}


def status_view(state) -> dict:
    return {
        "connected": getattr(state, "connected", False),
        "market_status": market_status(),
        "expiration": get_expiration_display(getattr(state, "expiration", "")) if getattr(state, "expiration", "") else "N/A",
        "data_mode": getattr(state, "data_mode", "initializing"),
        "gex_mode": getattr(state, "gex_mode", "0dte"),
        "spot": round(float(getattr(state, "spx_price", 0) or 0), 2),
        "vix": getattr(state, "vix", None),
    }


def strategies_view(state, name=None) -> dict:
    if name:
        s = getattr(state, "strategies", {}).get(name)
        if s is None:
            return {"ok": False, "error": f"Strategy '{name}' not found"}
        return {"ok": True, "name": name, "strategy": s.to_dict()}
    return {"ok": True,
            "strategies": [s.to_dict() for s in getattr(state, "strategies", {}).values()],
            "kill_switch": getattr(state, "auto_trade_kill_switch", False)}


def candidates_view(state, name) -> dict:
    strategies = getattr(state, "strategies", {})
    s = strategies.get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    cands = getattr(state, "strategy_candidates", {}).get(name) or build_candidate_views(s, state)
    if not cands:
        return {"ok": True, "name": name, "armed": s.armed, "auto_execute": s.auto_execute,
                "count": 0, "rows": [],
                "note": "No live candidates (strategy not armed, market closed, or no qualifying combo)."}
    right = "P" if s.direction == "bull_put" else "C"
    rows = [{
        "index": i,
        "direction": s.direction,
        "short_strike": c.get("short_strike"),
        "long_strike": c.get("long_strike"),
        "right": right,
        "credit_mid": c.get("credit_mid"),
        "short_delta": c.get("short_delta"),
        "width_points": c.get("width_points"),
        "size": c.get("size"),
        "total_credit": c.get("total_credit"),
        "total_margin": c.get("total_margin"),
        "auto_execute": s.auto_execute,
    } for i, c in enumerate(cands)]
    return {"ok": True, "name": name, "armed": s.armed, "auto_execute": s.auto_execute,
            "count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Actions (arm/disarm/kill-switch) — synchronous, safe, no order placement
# ---------------------------------------------------------------------------
def arm_strategy(state, name) -> dict:
    s = getattr(state, "strategies", {}).get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    s.armed = True
    reset_strategy_runtime(state, s.name)
    return {"ok": True, "name": s.name, "armed": True, "auto_execute": s.auto_execute,
            "direction": s.direction, "budget": s.budget,
            "kill_switch": getattr(state, "auto_trade_kill_switch", False)}


def disarm_strategy(state, name) -> dict:
    s = getattr(state, "strategies", {}).get(name)
    if s is None:
        return {"ok": False, "error": f"Strategy '{name}' not found"}
    s.armed = False
    return {"ok": True, "name": s.name, "armed": False}


def set_kill_switch(state, on: bool) -> bool:
    state.auto_trade_kill_switch = bool(on)
    return state.auto_trade_kill_switch


# ---------------------------------------------------------------------------
# Order refusal (Discord never places orders — confirm in web UI)
# ---------------------------------------------------------------------------
def place_refusal(state, name, index: int) -> dict:
    strategies = getattr(state, "strategies", {})
    s = strategies.get(name)
    if s is None:
        return {"ok": False, "message": f"Strategy '{name}' not found"}
    cands = getattr(state, "strategy_candidates", {}).get(name) or build_candidate_views(s, state)
    if not cands:
        return {"ok": False, "message": "No candidate to place — re-run /candidates."}
    c = cands[index] if 0 <= index < len(cands) else cands[0]
    right = "P" if s.direction == "bull_put" else "C"
    msg = (f"Order placement is not allowed from Discord. "
           f"Open the dashboard web UI to confirm this spread: "
           f"SELL {c.get('short_strike')}{right} / BUY {c.get('long_strike')}{right} "
           f"credit ~${c.get('credit_mid'):.2f} x{c.get('size', 1)}")
    return {"ok": False, "message": msg}


import collections
import time


class AlertBridge:
    """Observes WebSocket broadcasts and forwards a curated subset to Discord.

    ``classify``/``forward`` are deterministic and testable without a live bot:
    in tests, set ``poster`` to a callable that records ``(etype, data)``. In
    production, ``poster`` schedules an embed send on the bot's async loop.
    """

    def __init__(self, channel_id=None, poster=None, dedup_ttl=60.0, max_dedup=64):
        self.channel_id = channel_id
        self.poster = poster
        self.dedup_ttl = dedup_ttl
        self._seen = collections.OrderedDict()
        self._max_dedup = max_dedup
        self._last_connected = None
        self._last_kill_switch = None

    # -- dedup ---------------------------------------------------------------
    def _dedup_ok(self, key: str) -> bool:
        now = time.monotonic()
        while self._seen and now - self._seen[next(iter(self._seen))] > self.dedup_ttl:
            self._seen.popitem(last=False)
        if key in self._seen:
            return False
        self._seen[key] = now
        if len(self._seen) > self._max_dedup:
            self._seen.popitem(last=False)
        return True

    # -- classifier ----------------------------------------------------------
    def classify(self, message: dict):
        mtype = message.get("type")
        data = message.get("data") or {}
        if mtype == "order_status":
            key = f"order:{data.get('orderId')}:{data.get('status')}"
            return ("order_status", key, data) if self._dedup_ok(key) else None
        if mtype == "strategy_exit":
            key = f"exit:{data.get('name')}:{data.get('event')}"
            return ("strategy_exit", key, data) if self._dedup_ok(key) else None
        if mtype == "strategy_trigger":
            key = f"trigger:{data.get('name')}:{data.get('event')}"
            return ("strategy_trigger", key, data) if self._dedup_ok(key) else None
        if mtype == "ib_error":
            key = f"ib_error:{data.get('errorCode')}:{data.get('message')}"
            return ("ib_error", key, data) if self._dedup_ok(key) else None
        if mtype == "status":
            connected = data.get("connected")
            if connected is None:
                return None
            if self._last_connected is None:
                self._last_connected = connected   # seed; don't alert initial state
                return None
            if connected != self._last_connected:
                self._last_connected = connected
                key = f"conn:{connected}"
                return ("connection", key, data) if self._dedup_ok(key) else None
            return None
        if mtype == "strategy_list":
            ks = data.get("kill_switch")
            if ks is None:
                return None
            if self._last_kill_switch is None:
                self._last_kill_switch = ks         # seed
                return None
            if ks != self._last_kill_switch:
                self._last_kill_switch = ks
                key = f"killswitch:{ks}"
                return ("kill_switch", key, data) if self._dedup_ok(key) else None
            return None
        # high-frequency / non-actionable types are suppressed
        return None

    # -- dispatch ------------------------------------------------------------
    def forward(self, message: dict) -> None:
        if self.poster is None:
            return
        item = self.classify(message)
        if item is None:
            return
        etype, _key, data = item
        self.poster(etype, data)


import asyncio
import discord
from discord.ext import commands


class DiscordBot:
    """Thin discord.py wrapper around the command logic in this module.

    ``ib`` is held for future order paths and is currently unused by the
    read-only/arm commands (they read ``state`` directly, so a reconnected IB
    client never leaves the bot reading stale data).
    """

    def __init__(self, ib, state, token=None, guild_id=None, channel_id=None,
                 allowed_user_ids=None, allowed_role=None, poster=None):
        self.ib = ib
        self.state = state
        self.token = token or config.DISCORD_TOKEN
        self.allowed_user_ids = (list(allowed_user_ids) if allowed_user_ids is not None
                                 else list(config.DISCORD_ALLOWED_USER_IDS))
        self.allowed_role = (allowed_role if allowed_role is not None
                             else config.DISCORD_ALLOWED_ROLE)
        self.guild_id = (int(guild_id) if guild_id is not None
                         else (int(config.DISCORD_GUILD_ID) if str(config.DISCORD_GUILD_ID).isdigit() else None))
        intents = discord.Intents.default()
        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.guild_ids = [discord.Object(id=self.guild_id)] if self.guild_id else []
        self._channel_id = channel_id if channel_id is not None else config.DISCORD_CHANNEL_ID
        self.alert_bridge = AlertBridge(self._channel_id, poster=poster if poster is not None else self._default_poster)
        self._command_names = []
        self._alert_tasks = set()
        self._register_commands()

    @property
    def _default_poster(self):
        def poster(etype, data):
            if self._channel_id:
                # Always called from within the running loop (broadcast), so
                # schedule the send without blocking the broadcast path.
                task = asyncio.create_task(self._send_alert(etype, data))
                self._alert_tasks.add(task)
                task.add_done_callback(self._alert_tasks.discard)
        return poster

    async def _send_alert(self, etype, data):
        try:
            channel = await self.bot.fetch_channel(int(self._channel_id))
        except Exception as e:
            logger.warning(f"Discord alert channel {self._channel_id} fetch failed: {e}")
            return
        if channel is None:
            logger.warning(f"Discord alert channel {self._channel_id} not found")
            return
        try:
            await channel.send(embed=_embed(etype, data))
        except Exception as e:
            logger.error(f"Discord alert send failed: {e}")

    # -- registration --------------------------------------------------------
    def _register_commands(self):
        tb = self.bot.tree
        g = self.guild_ids
        names = []

        @tb.command(name="status", description="SPX 0DTE dashboard status", guilds=g)
        async def status_cmd(interaction: discord.Interaction):
            await self._respond(interaction, status_view, {})
        names.append("status")

        @tb.command(name="account", description="Account summary", guilds=g)
        async def account_cmd(interaction: discord.Interaction):
            await self._respond(interaction, account_view, {})
        names.append("account")

        @tb.command(name="positions", description="Portfolio positions", guilds=g)
        async def positions_cmd(interaction: discord.Interaction, filter: str = None):
            await self._respond(interaction, positions_view, {"filter": filter})
        names.append("positions")

        @tb.command(name="orders", description="Open orders", guilds=g)
        async def orders_cmd(interaction: discord.Interaction):
            await self._respond(interaction, orders_view, {})
        names.append("orders")

        @tb.command(name="strategy", description="Strategy list/detail", guilds=g)
        async def strategy_cmd(interaction: discord.Interaction, name: str = None):
            await self._respond(interaction, strategies_view, {"name": name})
        names.append("strategy")

        @tb.command(name="candidates", description="Live candidates for a strategy", guilds=g)
        async def candidates_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, candidates_view, {"name": name})
        names.append("candidates")

        @tb.command(name="arm", description="Arm a strategy", guilds=g)
        async def arm_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, arm_strategy, {"name": name})
        names.append("arm")

        @tb.command(name="disarm", description="Disarm a strategy", guilds=g)
        async def disarm_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, disarm_strategy, {"name": name})
        names.append("disarm")

        @tb.command(name="killswitch", description="Toggle the auto-trade kill switch", guilds=g)
        async def killswitch_cmd(interaction: discord.Interaction, on: bool):
            await self._respond(interaction, lambda st: {"ok": True, "kill_switch": set_kill_switch(st, on)}, {})
        names.append("killswitch")

        @tb.command(name="place", description="Refuse: place orders only via the web UI", guilds=g)
        async def place_cmd(interaction: discord.Interaction, name: str, index: int = 0):
            await self._respond(interaction, place_refusal, {"name": name, "index": index})
        names.append("place")

        @self.bot.event
        async def on_ready():
            if self.guild_id:
                await tb.sync(guild=discord.Object(id=self.guild_id))
            else:
                await tb.sync()

        self._command_names = names

    # -- response helper -----------------------------------------------------
    async def _respond(self, interaction, fn, kwargs):
        roles = getattr(interaction.user, "roles", None) or []
        if not is_authorized(interaction.user.id,
                             self.allowed_user_ids,
                             self.allowed_role,
                             [r.name for r in roles], [r.id for r in roles]):
            await interaction.response.send_message("Not authorized for this command.", ephemeral=True)
            return
        try:
            result = fn(self.state, **kwargs)
        except Exception as e:
            logger.error(f"command {interaction.command.name} failed: {e}")
            await interaction.response.send_message("Command failed. Check server logs.", ephemeral=True)
            return
        await interaction.response.send_message(embed=_embed(interaction.command.name, result))

    @property
    def command_names(self):
        return list(self._command_names)

    # -- lifecycle -----------------------------------------------------------
    async def start(self):
        await self.bot.start(self.token)

    async def close(self):
        for t in self._alert_tasks:
            t.cancel()
        await self.bot.close()


def make_discord_bot(ib, state, token=None, guild_id=None, channel_id=None,
                     allowed_user_ids=None, allowed_role=None, poster=None) -> DiscordBot:
    return DiscordBot(ib, state, token=token, guild_id=guild_id, channel_id=channel_id,
                      allowed_user_ids=allowed_user_ids, allowed_role=allowed_role,
                      poster=poster)


def _embed(title, result) -> discord.Embed:
    """Minimal embed builder: title + a text dump of the result dict."""
    embed = discord.Embed(title=title, color=0x00FF88)
    if isinstance(result, dict):
        text = _fmt(result)
    else:
        text = str(result)
    embed.description = text[:4000]
    return embed


def _fmt(d, indent=0):
    lines = []
    pad = "  " * indent
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            lines.append(_fmt(v, indent + 1))
        elif isinstance(v, list):
            lines.append(f"{pad}{k}:")
            for item in v:
                lines.append(f"{pad}  - {item}")
        else:
            lines.append(f"{pad}{k}: {v}")
    return "\n".join(lines)
