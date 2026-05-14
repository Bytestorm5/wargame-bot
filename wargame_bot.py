"""
Discord war-game timekeeping bot — multi-guild edition.

Concepts
--------
- Each Discord guild gets its own independent in-world clock.
- Real time flows normally. Per-guild, in-world time flows at a configurable
  pace, e.g. "1 year per real day" or "2 months 1 week 3 days per real day".
- Per guild we store: start_real, start_world, pace (relativedelta),
  paused, paused_at, paused_accumulated. Plus a list of reminders and at
  most one ticker.
- State lives in MongoDB. The bot can be restarted without losing anything.

Env vars
--------
  DISCORD_TOKEN     - Discord bot token (required)
  MONGODB_URI       - MongoDB connection string (required), e.g.
                      mongodb+srv://user:pw@cluster/?retryWrites=true
  MONGODB_DB        - Database name (default: wargame)

Run: DISCORD_TOKEN=... MONGODB_URI=... python wargame_bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks
from dateutil.relativedelta import relativedelta
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("wargame")

TICK_INTERVAL_SECONDS = 10  # how often we check reminders / tickers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pace and date parsing
# ---------------------------------------------------------------------------

_PACE_UNIT_RE = re.compile(
    r"(\d+)\s*(years?|yrs?|months?|mos?|weeks?|wks?|days?|y|mo|w|d)",
    re.IGNORECASE,
)

_UNIT_TO_KW = {
    "y": "years", "yr": "years", "yrs": "years", "year": "years", "years": "years",
    "mo": "months", "mos": "months", "month": "months", "months": "months",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
    "d": "days", "day": "days", "days": "days",
}


def parse_pace(text: str) -> relativedelta:
    matches = _PACE_UNIT_RE.findall(text)
    if not matches:
        raise ValueError(
            "Could not parse pace. Try e.g. '1 year', '2 months 1 week 3 days', '6mo'."
        )
    kwargs: dict[str, int] = {}
    for amount, unit in matches:
        key = _UNIT_TO_KW[unit.lower()]
        kwargs[key] = kwargs.get(key, 0) + int(amount)
    rd = relativedelta(**kwargs)
    sentinel = datetime(2000, 1, 1, tzinfo=timezone.utc)
    if sentinel + rd <= sentinel:
        raise ValueError("Pace must be a positive duration.")
    return rd


def format_pace(rd: relativedelta) -> str:
    parts = []
    if rd.years:
        parts.append(f"{rd.years} year{'s' if rd.years != 1 else ''}")
    if rd.months:
        parts.append(f"{rd.months} month{'s' if rd.months != 1 else ''}")
    total_days = rd.days  # relativedelta normalizes weeks-kw into days
    weeks, leftover_days = divmod(total_days, 7)
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if leftover_days:
        parts.append(f"{leftover_days} day{'s' if leftover_days != 1 else ''}")
    return " ".join(parts) if parts else "0 days"


def _rd_to_dict(rd: relativedelta) -> dict:
    return {
        "years": rd.years,
        "months": rd.months,
        "days": rd.days,
        "hours": rd.hours,
        "minutes": rd.minutes,
        "seconds": rd.seconds,
        "microseconds": rd.microseconds,
    }


def _rd_from_dict(d: dict) -> relativedelta:
    return relativedelta(**d)


_DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
]


def parse_date(text: str) -> datetime:
    """Parse a user-supplied in-world date. Returned as a UTC-tz datetime."""
    text = text.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as e:
        raise ValueError(
            "Could not parse date. Try YYYY-MM-DD, MM/DD/YYYY, or with HH:MM."
        ) from e


def format_world(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Per-guild clock model
# ---------------------------------------------------------------------------


@dataclass
class GuildClock:
    """
    A guild's clock state. Mirrors a document in the `clocks` collection
    where `_id = guild_id`.

    World time math:
        elapsed_days = (real_now - start_real - paused_total) / 1 day
        world_now    = start_world + elapsed_days * pace
    """
    guild_id: int
    start_real: datetime          # tz-aware UTC
    start_world: datetime         # tz-aware UTC (interpreted as in-world wall time)
    pace: relativedelta
    paused: bool = False
    paused_at: Optional[datetime] = None
    paused_accumulated_seconds: float = 0.0

    # --- serialization ---

    def to_doc(self) -> dict:
        return {
            "_id": self.guild_id,
            "start_real": self.start_real,
            "start_world": self.start_world,
            "pace": _rd_to_dict(self.pace),
            "paused": self.paused,
            "paused_at": self.paused_at,
            "paused_accumulated_seconds": self.paused_accumulated_seconds,
        }

    @classmethod
    def from_doc(cls, d: dict) -> "GuildClock":
        return cls(
            guild_id=d["_id"],
            start_real=_ensure_aware(d["start_real"]),
            start_world=_ensure_aware(d["start_world"]),
            pace=_rd_from_dict(d["pace"]),
            paused=d.get("paused", False),
            paused_at=_ensure_aware(d.get("paused_at")),
            paused_accumulated_seconds=d.get("paused_accumulated_seconds", 0.0),
        )

    # --- math ---

    def effective_real_elapsed(self, real_now: datetime) -> timedelta:
        elapsed = real_now - self.start_real
        elapsed -= timedelta(seconds=self.paused_accumulated_seconds)
        if self.paused and self.paused_at:
            elapsed -= (real_now - self.paused_at)
        if elapsed.total_seconds() < 0:
            elapsed = timedelta(0)
        return elapsed

    def world_time_at(self, real_dt: datetime) -> datetime:
        elapsed = self.effective_real_elapsed(real_dt)
        days_float = elapsed.total_seconds() / 86400.0
        return _advance_world(self.start_world, self.pace, days_float)

    def world_now(self) -> datetime:
        return self.world_time_at(_utcnow())


def _advance_world(start: datetime, pace: relativedelta, days_float: float) -> datetime:
    """
    Advance `start` by `pace * days_float`. The integer-day portion uses
    calendar-aware relativedelta multiplication; the fractional remainder is
    linearized within the *next* one-day-of-pace span.
    """
    if days_float <= 0:
        return start
    whole_days = int(days_float)
    frac = days_float - whole_days

    scaled = relativedelta(
        years=pace.years * whole_days,
        months=pace.months * whole_days,
        days=pace.days * whole_days,
        hours=pace.hours * whole_days,
        minutes=pace.minutes * whole_days,
        seconds=pace.seconds * whole_days,
        microseconds=pace.microseconds * whole_days,
    )
    after_whole = start + scaled
    if frac <= 0:
        return after_whole
    one_more = after_whole + pace
    one_day_span = (one_more - after_whole).total_seconds()
    return after_whole + timedelta(seconds=one_day_span * frac)


# ---------------------------------------------------------------------------
# Mongo store
# ---------------------------------------------------------------------------


class Store:
    """
    Three collections:
      clocks    {_id: guild_id, ...GuildClock fields...}
      reminders {_id: ObjectId, guild_id, user_id, channel_id,
                 world_target: Date, message: str}
      tickers   {_id: guild_id, channel_id, real_interval_seconds,
                 next_fire: Date}
    """

    def __init__(self, uri: str, db_name: str):
        # tz_aware=True ensures datetimes come back with tzinfo=UTC.
        self.client = AsyncIOMotorClient(uri, tz_aware=True)
        self.db = self.client[db_name]
        self.clocks = self.db["clocks"]
        self.reminders = self.db["reminders"]
        self.tickers = self.db["tickers"]

    async def setup(self) -> None:
        # tickers._id == guild_id, so uniqueness per guild is built in.
        await self.reminders.create_index([("guild_id", ASCENDING)])
        await self.reminders.create_index([("world_target", ASCENDING)])
        await self.tickers.create_index([("next_fire", ASCENDING)])

    # ---- clocks ----

    async def get_clock(self, guild_id: int) -> Optional[GuildClock]:
        doc = await self.clocks.find_one({"_id": guild_id})
        return GuildClock.from_doc(doc) if doc else None

    async def save_clock(self, clock: GuildClock) -> None:
        await self.clocks.replace_one(
            {"_id": clock.guild_id}, clock.to_doc(), upsert=True
        )

    async def all_clocks(self) -> dict[int, GuildClock]:
        result: dict[int, GuildClock] = {}
        async for doc in self.clocks.find({}):
            c = GuildClock.from_doc(doc)
            result[c.guild_id] = c
        return result

    # ---- reminders ----

    async def add_reminder(
        self,
        guild_id: int,
        user_id: int,
        channel_id: int,
        world_target: datetime,
        message: str,
    ) -> None:
        await self.reminders.insert_one({
            "guild_id": guild_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "world_target": world_target,
            "message": message,
        })

    async def delete_reminder(self, _id) -> None:
        await self.reminders.delete_one({"_id": _id})

    async def delete_reminders_for_guild(self, guild_id: int) -> None:
        await self.reminders.delete_many({"guild_id": guild_id})

    # ---- tickers ----

    async def get_ticker(self, guild_id: int) -> Optional[dict]:
        return await self.tickers.find_one({"_id": guild_id})

    async def set_ticker(
        self,
        guild_id: int,
        channel_id: int,
        real_interval_seconds: float,
        next_fire: datetime,
    ) -> None:
        await self.tickers.replace_one(
            {"_id": guild_id},
            {
                "_id": guild_id,
                "channel_id": channel_id,
                "real_interval_seconds": real_interval_seconds,
                "next_fire": next_fire,
            },
            upsert=True,
        )

    async def delete_ticker(self, guild_id: int) -> bool:
        result = await self.tickers.delete_one({"_id": guild_id})
        return result.deleted_count > 0

    async def update_ticker_next_fire(self, guild_id: int, next_fire: datetime) -> None:
        await self.tickers.update_one(
            {"_id": guild_id}, {"$set": {"next_fire": next_fire}}
        )


# Initialized in main()
store: Optional[Store] = None


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------


intents = discord.Intents.default()


class WarGameBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        background_loop.start()


bot = WarGameBot()


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


async def _require_guild(interaction: discord.Interaction) -> Optional[int]:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "❌ This command must be used in a server, not a DM.", ephemeral=True
        )
        return None
    return interaction.guild_id


async def _require_clock(interaction: discord.Interaction) -> Optional[GuildClock]:
    gid = await _require_guild(interaction)
    if gid is None:
        return None
    clock = await store.get_clock(gid)
    if clock is None:
        await interaction.response.send_message(
            "No clock set for this server. Use `/setclock` first.", ephemeral=True
        )
        return None
    return clock


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="setclock",
    description="Initialize this server's world clock with a start date and pace.",
)
@app_commands.describe(
    start_date="In-world start date, e.g. 1948-01-01 or 1/1/1948",
    pace="In-world time per real day, e.g. '1 year' or '2 months 1 week 3 days'",
)
async def setclock(interaction: discord.Interaction, start_date: str, pace: str):
    gid = await _require_guild(interaction)
    if gid is None:
        return
    try:
        start_world = parse_date(start_date)
        pace_rd = parse_pace(pace)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    clock = GuildClock(
        guild_id=gid,
        start_real=_utcnow(),
        start_world=start_world,
        pace=pace_rd,
    )
    await store.save_clock(clock)
    # A new clock invalidates prior reminders, whose in-world targets relate
    # to the previous timeline.
    await store.delete_reminders_for_guild(gid)
    await interaction.response.send_message(
        f"✅ Clock set for this server.\n"
        f"• Start (in-world): **{format_world(start_world)}**\n"
        f"• Pace: **{format_pace(pace_rd)}** per real day\n"
        f"• Running. (Prior reminders cleared.)"
    )


@bot.tree.command(name="start", description="Resume this server's clock if paused.")
async def start_cmd(interaction: discord.Interaction):
    clock = await _require_clock(interaction)
    if clock is None:
        return
    if not clock.paused:
        await interaction.response.send_message("Clock is already running.", ephemeral=True)
        return
    if clock.paused_at is not None:
        clock.paused_accumulated_seconds += (_utcnow() - clock.paused_at).total_seconds()
    clock.paused = False
    clock.paused_at = None
    await store.save_clock(clock)
    await interaction.response.send_message(
        f"▶️ Resumed. World time is now **{format_world(clock.world_now())}**."
    )


@bot.tree.command(name="pause", description="Stop this server's clock until resumed.")
async def pause_cmd(interaction: discord.Interaction):
    clock = await _require_clock(interaction)
    if clock is None:
        return
    if clock.paused:
        await interaction.response.send_message("Clock is already paused.", ephemeral=True)
        return
    clock.paused = True
    clock.paused_at = _utcnow()
    await store.save_clock(clock)
    await interaction.response.send_message(
        f"⏸️ Paused at **{format_world(clock.world_now())}**."
    )


@bot.tree.command(name="reset", description="Reset this server's clock back to its start date.")
async def reset_cmd(interaction: discord.Interaction):
    clock = await _require_clock(interaction)
    if clock is None:
        return
    clock.start_real = _utcnow()
    clock.paused_accumulated_seconds = 0.0
    clock.paused = False
    clock.paused_at = None
    await store.save_clock(clock)
    await store.delete_reminders_for_guild(clock.guild_id)
    await interaction.response.send_message(
        f"🔄 Reset. World time is back to **{format_world(clock.start_world)}** "
        f"(pace **{format_pace(clock.pace)}** per real day). Reminders cleared."
    )


@bot.tree.command(name="now", description="Show this server's current in-world time.")
async def now_cmd(interaction: discord.Interaction):
    clock = await _require_clock(interaction)
    if clock is None:
        return
    suffix = " (paused)" if clock.paused else ""
    await interaction.response.send_message(
        f"🕰️ **{format_world(clock.world_now())}**{suffix}\n"
        f"Pace: {format_pace(clock.pace)} per real day."
    )


@bot.tree.command(
    name="whenwas",
    description="Given a message link or ID, report the in-world time it was sent.",
)
@app_commands.describe(message="A message link, or a message ID from this channel.")
async def whenwas_cmd(interaction: discord.Interaction, message: str):
    clock = await _require_clock(interaction)
    if clock is None:
        return

    m = re.search(r"channels/\d+/(\d+)/(\d+)", message)
    if m:
        channel_id = int(m.group(1))
        message_id = int(m.group(2))
        channel = bot.get_channel(channel_id) or interaction.channel
    else:
        try:
            message_id = int(message.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Provide a message ID or message link.", ephemeral=True
            )
            return
        channel = interaction.channel

    try:
        msg = await channel.fetch_message(message_id)
        real_dt = msg.created_at
    except (discord.NotFound, discord.Forbidden, AttributeError):
        # Fallback: derive from snowflake directly.
        DISCORD_EPOCH = 1420070400000
        ms = (message_id >> 22) + DISCORD_EPOCH
        real_dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    real_dt = _ensure_aware(real_dt)
    world_dt = clock.world_time_at(real_dt)
    await interaction.response.send_message(
        f"📜 Message sent at <t:{int(real_dt.timestamp())}:F> (real)\n"
        f"In-world: **{format_world(world_dt)}**"
    )


@bot.tree.command(
    name="reminder",
    description="Remind you when an in-world time is reached (in this server).",
)
@app_commands.describe(
    message="What to remind you about.",
    delta="In-world delta from now (e.g. '3 months', '1 year 2 weeks').",
    date="Specific in-world date instead of a delta (e.g. 1950-06-15).",
)
async def reminder_cmd(
    interaction: discord.Interaction,
    message: str,
    delta: Optional[str] = None,
    date: Optional[str] = None,
):
    clock = await _require_clock(interaction)
    if clock is None:
        return
    if (delta is None) == (date is None):
        await interaction.response.send_message(
            "❌ Provide exactly one of `delta` or `date`.", ephemeral=True
        )
        return

    try:
        if delta is not None:
            target_world = clock.world_now() + parse_pace(delta)
        else:
            target_world = parse_date(date)  # type: ignore[arg-type]
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if target_world <= clock.world_now():
        await interaction.response.send_message(
            "❌ That in-world time is not in the future.", ephemeral=True
        )
        return

    await store.add_reminder(
        guild_id=clock.guild_id,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
        world_target=target_world,
        message=message,
    )
    await interaction.response.send_message(
        f"⏰ Reminder set for **{format_world(target_world)}** (in-world).\n"
        f"> {message}"
    )


@bot.tree.command(
    name="ticker",
    description="Auto-post the in-world time in this channel at a real-time cadence. One per server.",
)
@app_commands.describe(
    interval="Real-world interval, e.g. '24h', '30m', '1h30m'. Use 'off' to stop this server's ticker.",
)
async def ticker_cmd(interaction: discord.Interaction, interval: str):
    clock = await _require_clock(interaction)
    if clock is None:
        return

    gid = clock.guild_id

    if interval.strip().lower() in ("off", "stop", "none"):
        removed = await store.delete_ticker(gid)
        if removed:
            await interaction.response.send_message("🛑 Ticker stopped for this server.")
        else:
            await interaction.response.send_message(
                "No ticker was running for this server.", ephemeral=True
            )
        return

    try:
        seconds = parse_real_interval(interval)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if seconds < 30:
        await interaction.response.send_message(
            "❌ Minimum ticker interval is 30 seconds.", ephemeral=True
        )
        return

    existing = await store.get_ticker(gid)
    replaced_msg = ""
    if existing and existing.get("channel_id") != interaction.channel_id:
        replaced_msg = f" (Replaced previous ticker in <#{existing['channel_id']}>.)"

    next_fire = _utcnow() + timedelta(seconds=seconds)
    await store.set_ticker(
        guild_id=gid,
        channel_id=interaction.channel_id,
        real_interval_seconds=seconds,
        next_fire=next_fire,
    )
    await interaction.response.send_message(
        f"📡 Ticker set. Posting **/now** every {format_real_interval(seconds)} "
        f"in this channel. First post at <t:{int(next_fire.timestamp())}:T>.{replaced_msg}"
    )


_REAL_INTERVAL_RE = re.compile(
    r"(\d+)\s*(days?|hours?|minutes?|seconds?|hrs?|mins?|secs?|d|h|m|s)",
    re.IGNORECASE,
)


def parse_real_interval(text: str) -> float:
    matches = _REAL_INTERVAL_RE.findall(text)
    if not matches:
        raise ValueError("Could not parse interval. Try '24h', '30m', '1h30m'.")
    total = 0.0
    for amount, unit in matches:
        u = unit.lower()
        n = int(amount)
        if u.startswith("d"):
            total += n * 86400
        elif u.startswith("h"):
            total += n * 3600
        elif u.startswith("m") and not u.startswith("mo"):
            total += n * 60
        elif u.startswith("s"):
            total += n
    return total


def format_real_interval(seconds: float) -> str:
    s = int(seconds)
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if s >= size:
            q, s = divmod(s, size)
            parts.append(f"{q}{label}")
    return "".join(parts) or "0s"


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------


@tasks.loop(seconds=TICK_INTERVAL_SECONDS)
async def background_loop():
    if store is None:
        return
    try:
        await _process_tick()
    except Exception:
        log.exception("Background tick failed")


async def _process_tick():
    now_real = _utcnow()

    # Preload all clocks in one pass to avoid N+1 queries during processing.
    clocks_by_guild = await store.all_clocks()

    # --- Reminders: fire when their guild's world clock has reached the target. ---
    async for r in store.reminders.find({}):
        gid = r["guild_id"]
        clock = clocks_by_guild.get(gid)
        if clock is None:
            # No clock for this guild any more; drop orphaned reminder.
            await store.delete_reminder(r["_id"])
            continue
        world_target = _ensure_aware(r["world_target"])
        if clock.world_now() >= world_target:
            channel = bot.get_channel(r["channel_id"])
            if channel is not None:
                try:
                    await channel.send(
                        f"⏰ <@{r['user_id']}> reminder for "
                        f"**{format_world(world_target)}** (in-world): {r['message']}"
                    )
                except discord.HTTPException as e:
                    log.warning("Failed to send reminder: %s", e)
            await store.delete_reminder(r["_id"])

    # --- Tickers: real-time only; world clock state only affects displayed text. ---
    async for t in store.tickers.find({}):
        gid = t["_id"]
        clock = clocks_by_guild.get(gid)
        if clock is None:
            await store.delete_ticker(gid)
            continue
        next_fire = _ensure_aware(t["next_fire"])
        if now_real < next_fire:
            continue
        channel = bot.get_channel(t["channel_id"])
        if channel is not None:
            suffix = " (paused)" if clock.paused else ""
            try:
                await channel.send(f"🕰️ **{format_world(clock.world_now())}**{suffix}")
            except discord.HTTPException as e:
                log.warning("Failed to send ticker: %s", e)
        # Skip missed slots so we don't spam catch-up posts.
        interval = t["real_interval_seconds"]
        while next_fire <= now_real:
            next_fire += timedelta(seconds=interval)
        await store.update_ticker_next_fire(gid, next_fire)


@background_loop.before_loop
async def _before_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    log.info("Logged in as %s (id %s)", bot.user, bot.user.id if bot.user else "?")


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable.")
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise SystemExit("Set the MONGODB_URI environment variable.")
    db_name = os.environ.get("MONGODB_DB", "wargame")

    global store
    store = Store(uri, db_name)

    async def runner():
        await store.setup()
        async with bot:
            await bot.start(token)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
