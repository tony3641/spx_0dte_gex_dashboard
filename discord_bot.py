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
from discord.app_commands import CommandNotFound
from discord.ext import commands


class _LiquidateButton(discord.ui.Button):
    """A clickable Liquidate button bound to one position key.

    ``custom_id`` carries the position identity (Discord-safe: only letters,
    digits and hyphens) while the real poskey is held on the object so the
    callback can act on the live position it identifies.
    """

    def __init__(self, poskey, index, handler):
        super().__init__(style=discord.ButtonStyle.danger, label=f"Liquidate {index}",
                         custom_id=f"liquidate-{poskey}")
        self._poskey = poskey
        self._handler = handler

    async def callback(self, interaction):
        await self._handler(interaction, self._poskey)


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
        self._liquidating = set()   # position keys with a close order in flight
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
        # Commands are registered GLOBALLY, not guild-scoped.
        #
        # Guild-scoped commands (guilds=[...]) require the interaction's
        # data['guild_id'] to be present for discord.py's resolver
        # (_get_app_command_options) to find them. Discord omits guild_id from
        # the command-data payload, so a guild-scoped command raises
        # CommandNotFound on every invocation — the bot never runs the command
        # and Discord shows "The application did not respond". Global commands
        # live in the global registry, which the resolver checks as a fallback
        # regardless of guild_id, so they always resolve.
        tb = self.bot.tree
        names = []

        @tb.command(name="status", description="SPX 0DTE dashboard status")
        async def status_cmd(interaction: discord.Interaction):
            await self._respond(interaction, status_view, {})
        names.append("status")

        @tb.command(name="account", description="Account summary")
        async def account_cmd(interaction: discord.Interaction):
            await self._respond(interaction, account_view, {})
        names.append("account")

        @tb.command(name="positions", description="Portfolio positions")
        async def positions_cmd(interaction: discord.Interaction, filter: str = None):
            await self._respond(interaction, positions_view, {"filter": filter})
        names.append("positions")

        @tb.command(name="orders", description="Open orders")
        async def orders_cmd(interaction: discord.Interaction):
            await self._respond(interaction, orders_view, {})
        names.append("orders")

        @tb.command(name="strategy", description="Strategy list/detail")
        async def strategy_cmd(interaction: discord.Interaction, name: str = None):
            await self._respond(interaction, strategies_view, {"name": name})
        names.append("strategy")

        @tb.command(name="candidates", description="Live candidates for a strategy")
        async def candidates_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, candidates_view, {"name": name})
        names.append("candidates")

        @tb.command(name="arm", description="Arm a strategy")
        async def arm_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, arm_strategy, {"name": name})
        names.append("arm")

        @tb.command(name="disarm", description="Disarm a strategy")
        async def disarm_cmd(interaction: discord.Interaction, name: str):
            await self._respond(interaction, disarm_strategy, {"name": name})
        names.append("disarm")

        @tb.command(name="killswitch", description="Toggle the auto-trade kill switch")
        async def killswitch_cmd(interaction: discord.Interaction, on: bool):
            await self._respond(interaction, lambda st: {"ok": True, "kill_switch": set_kill_switch(st, on)}, {})
        names.append("killswitch")

        @tb.command(name="place", description="Refuse: place orders only via the web UI")
        async def place_cmd(interaction: discord.Interaction, name: str, index: int = 0):
            await self._respond(interaction, place_refusal, {"name": name, "index": index})
        names.append("place")

        @self.bot.event
        async def on_ready():
            await tb.sync()

        # Any app-command error (incl. CommandNotFound from a guild/scope mismatch)
        # must respond to the interaction; discord.py's default on_error only logs
        # and lets Discord time out with "The application did not respond".
        tb.on_error = self._on_tree_error

        self._command_names = names

    # -- response helper -----------------------------------------------------
    def _interaction_name(self, interaction) -> str:
        """Best-effort command name. ``interaction.command`` is a cached lookup that
        can be ``None`` (e.g. CommandNotFound before the callback runs), so fall
        back to the raw ``data['name']`` instead of calling ``.name`` unguarded."""
        cmd = getattr(interaction, "command", None)
        if cmd is not None:
            return getattr(cmd, "name", "command") or "command"
        data = interaction.data or {}
        return data.get("name", "command") or "command"

    async def _safe_respond(self, interaction, content=None, *, embed=None, view=None, ephemeral=True):
        """Attempt one interaction response; never raise.

        Discord drops an interaction that receives no callback within 3s with the
        message "The application did not respond". Every error path funnels through
        here so a failure can never silently time out the interaction.
        """
        if content is None and embed is None:
            return
        try:
            if embed is not None:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content or "Command failed.", view=view, ephemeral=ephemeral)
        except discord.InteractionResponded:
            pass
        except Exception as e:
            logger.error(f"Failed to respond to Discord interaction: {e}")

    def _friendly_command_error(self, name, error, guild_id) -> str:
        if isinstance(error, CommandNotFound):
            return (
                f"The /{name} command is registered for a different server, so it can't be "
                f"resolved here.\n\nThis usually happens when commands were synced while "
                f"DISCORD_GUILD_ID was empty or pointed at another server. Have the bot owner "
                f"re-sync the commands in the server it's configured for "
                f"(guild {guild_id or '?'})."
            )
        return f"The /{name} command ran into an error. The bot owner can find the full details in the dashboard Log tab."

    async def _on_tree_error(self, interaction, error):
        """Application-command error handler: surface the failure instead of leaving the
        interaction unanswered (which Discord reports as 'The application did not respond')."""
        name = self._interaction_name(interaction)
        guild_id = getattr(interaction, "guild_id", None) or (interaction.data or {}).get("guild_id")
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        logger.error("Discord command error: /%s (guild_id=%s user_id=%s): %s",
                     name, guild_id, user_id, error, exc_info=error)
        await self._safe_respond(interaction, self._friendly_command_error(name, error, guild_id))

    async def _respond(self, interaction, fn, kwargs):
        name = self._interaction_name(interaction)
        guild_id = getattr(interaction, "guild_id", None)
        logger.debug("Discord command /%s from user %s in guild %s",
                     name, getattr(interaction.user, "id", None), guild_id)
        roles = getattr(interaction.user, "roles", None) or []
        try:
            if not is_authorized(interaction.user.id,
                                 self.allowed_user_ids,
                                 self.allowed_role,
                                 [r.name for r in roles], [r.id for r in roles]):
                await self._safe_respond(interaction, "Not authorized for this command.")
                return
            result = fn(self.state, **kwargs)
        except Exception as e:
            logger.error(f"command {name} failed: {e}", exc_info=True)
            await self._safe_respond(interaction, "Command failed. Check server logs.")
            return
        await self._safe_respond(interaction, embed=_embed(name, result),
                                 view=self._build_view(name, result), ephemeral=False)

    # -- position liquidation (interaction buttons) --------------------------
    def _build_view(self, name, result):
        """Return a discord.ui.View (buttons) for a command result, or None."""
        if name == "positions":
            return self._build_positions_view(result)
        return None

    def _build_positions_view(self, result):
        """One Liquidate button per liquidatable position, in row order.

        The button label carries the same row index as the table's ``#`` column
        so clicking one unambiguously targets that row's position.
        """
        positions = result.get("positions") or []
        view = discord.ui.View(timeout=300)
        for i, p in enumerate(positions, start=1):
            c = p.get("contract") or {}
            if not _liquidatable(c, p.get("position")):
                continue
            view.add_item(_LiquidateButton(_position_key(c), i, self._handle_liquidate))
        return view if view.children else None

    def _find_position(self, poskey):
        for p in getattr(self.state, "positions", []) or []:
            if _position_key(p.get("contract") or {}) == poskey:
                return p
        return None

    async def _liquidate_poskey(self, poskey):
        """Place a close order for the position identified by ``poskey``.

        Returns (ok, message). Uses the same close payload as the web UI and the
        shared handle_place_order path; dedupes via ``self._liquidating``.
        """
        pos = self._find_position(poskey)
        if pos is None:
            return False, "Position not found."
        if poskey in self._liquidating:
            return False, "Already liquidating — a close order is in flight."
        payload = _liquidation_payload(pos.get("contract"), pos.get("position"))
        if payload is None:
            return False, "Not a liquidatable position (OPT or STK required)."
        self._liquidating.add(poskey)
        from order_manager import handle_place_order
        try:
            resp = await handle_place_order(self.ib, self.state, payload)
        except Exception as e:
            self._liquidating.discard(poskey)
            return False, f"Liquidation failed: {e}"
        data = (resp or {}).get("data", {}) or {}
        status = data.get("status", "Submitted")
        if status == "Error":
            self._liquidating.discard(poskey)
            return False, f"Liquidation failed: {data.get('message', 'Unknown error')}"
        sym = (pos.get("contract") or {}).get("symbol", "")
        return True, f"{sym} liquidation {status.lower()}"

    async def _handle_liquidate(self, interaction, poskey):
        """Button callback: authorize, defer, place the close order, follow up."""
        roles = getattr(interaction.user, "roles", None) or []
        if not is_authorized(interaction.user.id, self.allowed_user_ids,
                             self.allowed_role, [r.name for r in roles],
                             [r.id for r in roles]):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass
        ok, msg = await self._liquidate_poskey(poskey)
        text = msg if ok else f"⚠️ {msg}"
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception as e:
            logger.error(f"liquidate followup failed: {e}")
            try:
                await interaction.response.send_message(text, ephemeral=True)
            except Exception:
                pass

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


# ---------------------------------------------------------------------------
# Text formatting helpers (pure, no discord dependency — unit-testable)
# ---------------------------------------------------------------------------
def _money(x) -> str:
    if x is None:
        return ""
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return ""


def _pct(x) -> str:
    if x is None:
        return ""
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def _badge(on) -> str:
    return f"{'🟢' if on else '⚫'} {'ON' if on else 'OFF'}"


def _num_cell(x) -> str:
    """Render a possibly-float numeric cell without trailing .0; '' for None/err.

    Whole numbers get thousands separators (e.g. strike 7650.0 -> '7,650') so
    margins and strikes read clean in a table; floats keep their decimals.
    """
    if x is None:
        return ""
    if isinstance(x, float) and x.is_integer():
        return f"{int(x):,}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _money_cell(x) -> str:
    """Two-decimal money cell for table columns; '' for None/err."""
    if x is None:
        return ""
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return ""


def _table(headers, rows) -> str:
    """Render an aligned monospace table with a header separator.

    Each column is right-aligned when every non-empty cell is numeric, else
    left-aligned. Columns are joined by two spaces. Returns the bare table
    (no code fence); callers wrap it.
    """
    headers = list(headers)
    rows = [list(r) for r in rows]
    ncols = len(headers)
    widths = [len(str(h)) for h in headers]

    def _is_num(v) -> bool:
        if v is None:
            return False
        return isinstance(v, (int, float))

    # Column widths from the widest cell; numeric detection per column.
    numeric = [True] * ncols
    for row in rows:
        for i in range(ncols):
            v = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], len(_num_cell(v)) if v is not None else 0)
            if v is not None and not _is_num(v):
                numeric[i] = False
    for i in range(ncols):
        if not headers:
            break

    def _pad(cell, w, right):
        cell = _num_cell(cell)
        return cell.rjust(w) if right else cell.ljust(w)

    lines = ["  ".join(_pad(headers[i], widths[i], numeric[i]) for i in range(ncols))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        cells = list(row) + [""] * (ncols - len(row))
        lines.append("  ".join(
            _pad(cells[i], widths[i], numeric[i]) for i in range(ncols)))
    return "\n".join(lines)


def _fence(text: str) -> str:
    return f"```{text}```"


# ---------------------------------------------------------------------------
# Per-command formatters: turn a *_view dict into readable Discord text.
# Each returns a plain str (survives .strip()/substring checks without a bot).
# ---------------------------------------------------------------------------
# Cap tables so a long candidate list can't overflow Discord's ~4k description.
_MAX_TABLE_ROWS = 50


def _fmt_candidates(result) -> str:
    if not result.get("ok", False):
        return _fmt_error_text(result)
    rows = result.get("rows") or []
    name = result.get("name", "")
    armed = result.get("armed", False)
    auto = result.get("auto_execute", False)
    count = result.get("count", len(rows))
    if not rows:
        return (f"**{name}**\n"
                f"{_badge(armed)} armed   {_badge(auto)} auto-execute\n"
                f"No live candidates. Arm the strategy or wait for market hours to re-run /candidates.")
    headers = ["id", "dir", "SELL", "BUY", "W", "CRED", "DELTA", "SZ", "TCRED", "MGN"]
    body = []
    shown = rows[:_MAX_TABLE_ROWS]
    for r in shown:
        body.append([
            r.get("index", ""),
            r.get("right", ""),
            _num_cell(r.get("short_strike")),
            _num_cell(r.get("long_strike")),
            _num_cell(r.get("width_points")),
            _money_cell(r.get("credit_mid")),
            _pct(r.get("short_delta")),
            _num_cell(r.get("size")),
            _money_cell(r.get("total_credit")),
            _num_cell(r.get("total_margin")),
        ])
    tbl = _table(headers, body)
    note = "" if len(rows) <= _MAX_TABLE_ROWS else f"\n… {len(rows) - len(shown)} more (truncated)"
    return (f"**{name}**   {_badge(armed)} armed   {_badge(auto)} auto-execute   "
            f"📊 {count} rows\n"
            f"{_fence(tbl)}{note}\n"
            f"Confirm entries in the web UI — Discord never places orders.")


def _fmt_error_text(result) -> str:
    msg = result.get("error") or result.get("message") or "Unknown error."
    return f"🔴 {msg}"


def _fmt_strategy(result) -> str:
    if not result.get("ok", False):
        return _fmt_error_text(result)
    if "strategy" in result:
        s = result["strategy"]
        conds = s.get("conditions") or []
        rows = [[c.get("kind", ""), _badge(c.get("enabled", True)), ",".join(
            f"{k}={v}" for k, v in (c.get("params") or {}).items())]
            for c in conds]
        cond_tbl = _table(["condition", "on", "params"], rows) if rows else "none"
        run_days = s.get("run_days") or []
        run_line = _run_days_line(run_days)
        budget = s.get("budget")
        width = f"${budget:,.0f}" if isinstance(budget, (int, float)) else "-"
        exit_rules = s.get("exit_rules") or {}
        tp = exit_rules.get("take_profit") or {}
        sl = exit_rules.get("stop_loss") or {}
        return (
            f"**{s.get('name', '')}**   {_badge(s.get('armed', False))} armed  "
            f"{_badge(s.get('auto_execute', False))} auto\n"
            f"Direction: **{s.get('direction', '')}**   Budget: {width}   Expiry: {s.get('target_expiry', '-')}\n"
            f"Run days: {run_line}   Trigger: {s.get('trigger_logic', 'any')}\n"
            f"Exit: TP {tp.get('mode', 'pct_credit')} {tp.get('value', 0)} / "
            f"SL x{sl.get('multiplier', 1)}  hold_to_expire={exit_rules.get('hold_to_expire', False)}\n"
            f"Conditions ({len(conds)}):\n{_fence(cond_tbl)}"
        )
    strategies = result.get("strategies") or []
    kill = result.get("kill_switch", False)
    lines = [f"🛑 Kill-Switch: {_badge(kill)}   📊 {len(strategies)} strategies"]
    rows = [[s.get("name", ""), s.get("direction", ""),
             _badge(s.get("armed", False)), _badge(s.get("auto_execute", False))] for s in strategies]
    if rows:
        lines.append(_fence(_table(["name", "direction", "armed", "auto"], rows)))
    return "\n".join(lines)


def _fmt_status(result) -> str:
    vix = result.get("vix")
    return (f"Market: **{result.get('market_status', '?')}**\n"
            f"Expiration: {result.get('expiration', '-')}   "
            f"Data: {result.get('data_mode', '?')}   GEX: {result.get('gex_mode', '?')}\n"
            f"{'🟢' if result.get('connected') else '🔴'} connected\n"
            f"SPX: **{_money(result.get('spot'))}**   VIX: **{vix if vix is not None else '-'}**")


def _opt_name(contract) -> str:
    """Human name for a position contract: 'SPX Jun26 7700 CALL' or 'QCOM' for stocks."""
    if not isinstance(contract, dict):
        return "-"
    sym = contract.get("symbol") or "-"
    expiry = contract.get("expiry") or ""
    strike = contract.get("strike")
    right = (contract.get("right") or "").strip()
    sec_type = contract.get("secType") or ""
    # Options carry expiry+strike+right; stocks don't.
    if sec_type == "OPT" and expiry and strike is not None and right:
        try:
            from datetime import datetime
            mon_yr = datetime.strptime(expiry, "%Y%m%d").strftime("%b%y")
        except (ValueError, TypeError):
            mon_yr = expiry
        strike_str = f"{strike:.0f}" if isinstance(strike, (int, float)) else str(strike)
        full = "CALL" if right.upper().startswith("C") else "PUT"
        return f"{sym} {mon_yr} {strike_str} {full}"
    if contract.get("localSymbol") and contract.get("localSymbol") != sym:
        return str(contract.get("localSymbol"))
    return sym


# -- Position identity + close-order payload (used by the Liquidate buttons) --
def _position_key(contract) -> str:
    """Stable, Discord-safe identity for a position ('SPX-OPT-20260626-7700-C').

    Used as a button custom_id and to re-locate the live position. Components
    are stripped to alphanumerics so the string is a valid custom_id.
    """
    import re
    safe = lambda v: re.sub(r"[^A-Za-z0-9]", "", str(v or ""))
    c = contract or {}
    sym = safe(c.get("symbol"))
    st = safe(c.get("secType"))
    ex = safe(c.get("expiry"))
    strike = c.get("strike")
    sk = safe(int(strike) if isinstance(strike, (int, float)) and strike else strike)
    right = safe(c.get("right"))
    return "-".join([sym, st, ex, sk, right])


def _liquidatable(contract, position) -> bool:
    """True when the position can be liquidated (OPT or STK, non-zero size)."""
    c = contract or {}
    sec_type = c.get("secType")
    if position in (None, 0):
        return False
    if sec_type not in ("OPT", "STK") or not c.get("symbol"):
        return False
    if sec_type == "OPT":
        if not (c.get("expiry") and c.get("strike") is not None and c.get("right")):
            return False
    return True


def _liquidation_payload(contract, position) -> Optional[dict]:
    """Build the same close-order payload the web UI sends (adaptive mid fill).

    Returns None when the contract can't be liquidated. Close action flips the
    sign of the held position (short -> BUY, long -> SELL).
    """
    if not _liquidatable(contract, position):
        return None
    c = contract or {}
    sec_type = c.get("secType")
    close_action = "SELL" if position > 0 else "BUY"
    leg = {"symbol": c.get("symbol"), "action": close_action, "qty": abs(int(position)),
           "lmtPrice": None, "secType": sec_type}
    if sec_type == "OPT":
        leg["expiry"] = c.get("expiry")
        leg["strike"] = c.get("strike")
        leg["right"] = c.get("right")
    return {"legs": [leg], "orderType": "LMT", "tif": "DAY", "outsideRth": True,
            "dynamicFill": True, "repriceIntervalSec": 0.3}


_RUN_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _run_days_line(days) -> str:
    """Render run days as '[ Mon | Tue | ... ]' with selected days bolded."""
    selected = set(int(d) for d in (days or []) if d is not None and str(d).isdigit())
    parts = []
    for i, label in enumerate(_RUN_DAYS):
        cell = f"**{label}**" if i in selected else label
        parts.append(cell)
    return "[ " + " | ".join(parts) + " ]"


def _fmt_account(result) -> str:
    summary = result.get("summary") or {}
    positions = result.get("positions") or []
    cash = summary.get("TotalCashValue")
    excess = summary.get("ExcessLiquidity")
    un = summary.get("UnrealizedPnL")
    re = summary.get("RealizedPnL")
    return (f"💰 Net Liq: **{_money(summary.get('NetLiquidation'))}**  "
            f"Cash: **{_money(cash)}**  Excess: **{_money(excess)}**\n"
            f"Buying Power: **{_money(summary.get('BuyingPower'))}**  "
            f"Unrealized PnL: **{_money(un)}**  Realized PnL: **{_money(re)}**\n"
            f"📈 {len(positions)} position{'s' if len(positions) != 1 else ''}"
            f"   {len(result.get('executions') or [])} executions")


def _fmt_orders(result) -> str:
    orders = result.get("orders") or []
    headers = ["id", "action", "qty", "status", "type"]
    rows = [[o.get("orderId", ""), o.get("action", ""), o.get("totalQuantity", ""),
             o.get("status", ""), o.get("orderType", "")] for o in orders]
    block = _fence(_table(headers, rows)) if rows else "none"
    count = result.get("count", len(orders))
    return f"📦 {count} open order{'s' if count != 1 else ''}\n{block}"


def _fmt_positions(result) -> str:
    positions = result.get("positions") or []
    # A `#` index column so each row maps to the matching "Liquidate #n" button
    # below (a code-block cell can't be a clickable order link).
    headers = ["#", "contract", "qty", "avg cost", "unrlzd pnl"]
    rows = []
    for i, p in enumerate(positions, start=1):
        c = p.get("contract") or {}
        rows.append([i, _opt_name(c), p.get("position"),
                     _money(p.get("averageCost")), _money(p.get("unrealizedPNL"))])
    block = _fence(_table(headers, rows)) if rows else "none"
    count = result.get("count", len(positions))
    return f"💼 {count} position{'s' if count != 1 else ''}\n{block}"


def _fmt_alert(etype, data) -> str:
    """Render a WebSocket broadcast alert as a short, readable line."""
    if not isinstance(data, dict):
        return str(data)
    if etype == "order_status":
        status = data.get("status", "?")
        oid = data.get("orderId", "")
        action = data.get("action", "")
        qty = data.get("totalQuantity", "")
        sym = (data.get("contract") or {}).get("symbol", "")
        return " ".join(p for p in (
            f"📦 Order **{oid}** {status} —", action, f"{qty} {sym}".strip()) if p).rstrip()
    if etype == "strategy_exit":
        price = data.get("price")
        at_price = f" at ${price:,.2f}" if price is not None else ""
        return (f"🚪 Exit **{data.get('name', '')}** — "
                f"{data.get('event', 'closed')}{at_price}")
    if etype == "strategy_trigger":
        return (f"⚡ Trigger **{data.get('name', '')}** — {data.get('event', 'fired')} "
                f"({data.get('trigger', '')})")
    if etype == "connection":
        on = bool(data.get("connected"))
        return f"{'🟢' if on else '🔴'} {'Connected' if on else 'Disconnected'}"
    if etype == "kill_switch":
        on = bool(data.get("kill_switch"))
        return f"{'🛑' if on else '▶️'} Kill-Switch: {_badge(on)}"
    if etype == "ib_error":
        return (f"⚠️ IB error {data.get('errorCode', '')}: "
                f"{data.get('message', '')}".rstrip())
    if etype == "status":
        return _fmt_status(data)
    return _fmt(data)


def _fmt_action(title, result) -> str:
    if not result.get("ok", False):
        return _fmt_error_text(result)
    if title == "arm":
        return (f"**{result.get('name', '')}** armed → {_badge(result.get('armed', False))}   "
                f"auto {_badge(result.get('auto_execute', False))}   "
                f"budget ${result.get('budget', '-')}")
    if title == "disarm":
        return f"**{result.get('name', '')}** disarmed → {_badge(False)}"
    if title == "killswitch":
        ks = result.get("kill_switch", False)
        return f"{'🛑' if ks else '▶️'} Kill-Switch: {_badge(ks)}"
    if title == "place":
        return _fmt_error_text(result)
    return _fmt(result)


# ---------------------------------------------------------------------------
# Embed orchestration: pick a color + formatter by command/event title.
# ---------------------------------------------------------------------------
def _embed_color(result) -> int:
    if not isinstance(result, dict):
        return 0x00FF88
    # Error results (ok:False) and a triggered kill-switch are loud red.
    if result.get("kill_switch"):
        return 0xFF4136
    if result.get("ok") is False:
        return 0xFF4136
    # Empty candidates: nothing actionable — amber rather than green.
    if "candidates" in result or "count" in result:
        if not result.get("rows"):
            return 0xFFB347
    return 0x00FF88


_FORMATTERS = {
    "candidates": _fmt_candidates,
    "strategy": _fmt_strategy,
    "status": _fmt_status,
    "account": _fmt_account,
    "orders": _fmt_orders,
    "positions": _fmt_positions,
    "arm": _fmt_action,
    "disarm": _fmt_action,
    "killswitch": _fmt_action,
    "place": _fmt_action,
    # WebSocket broadcast alerts
    "order_status": _fmt_alert,
    "strategy_exit": _fmt_alert,
    "strategy_trigger": _fmt_alert,
    "connection": _fmt_alert,
    "kill_switch": _fmt_alert,
    "ib_error": _fmt_alert,
}


def _embed(title, result) -> discord.Embed:
    """Build a readable embed for a command/event result.

    The view functions return data structures (they also feed the web UI); this
    is the only place they become human text. Unknown titles fall back to the
    dict dump below.
    """
    color = _embed_color(result)
    formatter = _FORMATTERS.get(title)
    if formatter is not None and isinstance(result, dict):
        # _fmt_action and _fmt_alert need the title (command/event) as context.
        text = formatter(title, result) if formatter in (_fmt_action, _fmt_alert) \
            else formatter(result)
    elif isinstance(result, dict):
        text = _fmt(result)
    else:
        text = str(result)
    embed = discord.Embed(title=title, color=color)
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
