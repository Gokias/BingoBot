import os
import re
import sqlite3
import asyncio
import random
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "./data/bingobot.sqlite")
DATA_DIR = os.getenv("DATA_DIR", "./data")

SIGNUP_EMOJI = "✅"
UNSIGN_EMOJI = "❌"
APPROVE_EMOJI = "👍"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bingobot")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "boards"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "submissions"), exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_channel_mention(text: str) -> Optional[int]:
    m = re.match(r"^<#!?(\d+)>$", text.strip())
    return int(m.group(1)) if m else None


def parse_role_mention(text: str) -> Optional[int]:
    t = text.strip()
    if t.lower() in ("none", "no", "n/a"):
        return None
    m = re.match(r"^<@&(\d+)>$", t)
    return int(m.group(1)) if m else None


def parse_int(text: str, min_value: Optional[int] = None, max_value: Optional[int] = None) -> Optional[int]:
    try:
        v = int(text.strip())
    except ValueError:
        return None
    if min_value is not None and v < min_value:
        return None
    if max_value is not None and v > max_value:
        return None
    return v


def parse_dt_with_tz(dt_str: str, tz_str: str) -> Optional[datetime]:
    """
    Accepts dt_str like: 2026-01-17 19:00
    tz_str like: America/New_York
    Returns UTC-aware datetime.
    """
    try:
        tz = ZoneInfo(tz_str.strip())
        naive = datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M")
        aware = naive.replace(tzinfo=tz)
        return aware.astimezone(timezone.utc)
    except Exception:
        return None


@dataclass
class GuildSettings:
    guild_id: int
    signup_channel_id: int
    submissions_channel_id: int
    announcements_channel_id: int
    board_channel_id: int


class BingoDB:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._conn() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    signup_channel_id INTEGER NOT NULL,
                    submissions_channel_id INTEGER NOT NULL,
                    announcements_channel_id INTEGER NOT NULL,
                    board_channel_id INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bingos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    game_mode TEXT NOT NULL,
                    team_size INTEGER NOT NULL,
                    team_mode TEXT NOT NULL,
                    notify_role_id INTEGER,
                    start_utc TEXT NOT NULL,
                    end_utc TEXT,
                    signup_close_utc TEXT NOT NULL,
                    show_board_when TEXT NOT NULL, -- signup_created | signups_close | bingo_start
                    approvals_mode TEXT NOT NULL,  -- none | admin | nonteammate (nonteammate reserved)
                    approvals_required INTEGER NOT NULL,
                    status TEXT NOT NULL,          -- setup | signup_open | signup_closed | running | ended
                    signup_message_id INTEGER,
                    leaderboard_message_id INTEGER,
                    board_image_path TEXT,
                    created_by_user_id INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signups (
                    bingo_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    joined_at_utc TEXT NOT NULL,
                    PRIMARY KEY (bingo_id, user_id),
                    FOREIGN KEY (bingo_id) REFERENCES bingos(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS teams (
                    bingo_id INTEGER NOT NULL,
                    team_number INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (bingo_id, user_id),
                    FOREIGN KEY (bingo_id) REFERENCES bingos(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bingo_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    kind TEXT NOT NULL, -- progress | full_tile
                    created_at_utc TEXT NOT NULL,
                    message_id INTEGER NOT NULL, -- message in submissions channel
                    attachment_path TEXT NOT NULL,
                    status TEXT NOT NULL, -- pending | approved | rejected
                    approvals_required INTEGER NOT NULL,
                    FOREIGN KEY (bingo_id) REFERENCES bingos(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    submission_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    approved_at_utc TEXT NOT NULL,
                    PRIMARY KEY (submission_id, user_id),
                    FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
                );
                """
            )

    # -------- guild settings --------
    def upsert_guild_settings(self, gs: GuildSettings) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO guild_settings (guild_id, signup_channel_id, submissions_channel_id, announcements_channel_id, board_channel_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    signup_channel_id=excluded.signup_channel_id,
                    submissions_channel_id=excluded.submissions_channel_id,
                    announcements_channel_id=excluded.announcements_channel_id,
                    board_channel_id=excluded.board_channel_id;
                """,
                (gs.guild_id, gs.signup_channel_id, gs.submissions_channel_id, gs.announcements_channel_id, gs.board_channel_id),
            )

    def get_guild_settings(self, guild_id: int) -> Optional[GuildSettings]:
        with self._conn() as con:
            row = con.execute("SELECT * FROM guild_settings WHERE guild_id=?;", (guild_id,)).fetchone()
            if not row:
                return None
            return GuildSettings(
                guild_id=row["guild_id"],
                signup_channel_id=row["signup_channel_id"],
                submissions_channel_id=row["submissions_channel_id"],
                announcements_channel_id=row["announcements_channel_id"],
                board_channel_id=row["board_channel_id"],
            )

    # -------- bingo lifecycle --------
    def get_active_bingo(self, guild_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as con:
            row = con.execute(
                """
                SELECT * FROM bingos
                WHERE guild_id=? AND status IN ('setup','signup_open','signup_closed','running')
                ORDER BY id DESC LIMIT 1;
                """,
                (guild_id,),
            ).fetchone()
            return row

    def create_bingo(
        self,
        guild_id: int,
        name: str,
        game_mode: str,
        team_size: int,
        team_mode: str,
        notify_role_id: Optional[int],
        start_utc: datetime,
        end_utc: Optional[datetime],
        signup_close_utc: datetime,
        show_board_when: str,
        approvals_mode: str,
        approvals_required: int,
        created_by_user_id: int,
        board_image_path: Optional[str],
    ) -> int:
        with self._conn() as con:
            cur = con.execute(
                """
                INSERT INTO bingos (
                    guild_id, name, game_mode, team_size, team_mode, notify_role_id,
                    start_utc, end_utc, signup_close_utc, show_board_when,
                    approvals_mode, approvals_required, status,
                    board_image_path, created_by_user_id, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'signup_open', ?, ?, ?);
                """,
                (
                    guild_id,
                    name,
                    game_mode,
                    team_size,
                    team_mode,
                    notify_role_id,
                    start_utc.isoformat(),
                    end_utc.isoformat() if end_utc else None,
                    signup_close_utc.isoformat(),
                    show_board_when,
                    approvals_mode,
                    approvals_required,
                    board_image_path,
                    created_by_user_id,
                    utc_now().isoformat(),
                ),
            )
            return int(cur.lastrowid)

    def set_signup_message_id(self, bingo_id: int, message_id: int) -> None:
        with self._conn() as con:
            con.execute("UPDATE bingos SET signup_message_id=? WHERE id=?;", (message_id, bingo_id))

    def set_leaderboard_message_id(self, bingo_id: int, message_id: int) -> None:
        with self._conn() as con:
            con.execute("UPDATE bingos SET leaderboard_message_id=? WHERE id=?;", (message_id, bingo_id))

    def set_status(self, bingo_id: int, status: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE bingos SET status=? WHERE id=?;", (status, bingo_id))

    # -------- signups --------
    def add_signup(self, bingo_id: int, user_id: int) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO signups (bingo_id, user_id, joined_at_utc)
                VALUES (?, ?, ?);
                """,
                (bingo_id, user_id, utc_now().isoformat()),
            )

    def remove_signup(self, bingo_id: int, user_id: int) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM signups WHERE bingo_id=? AND user_id=?;", (bingo_id, user_id))

    def list_signups(self, bingo_id: int) -> List[int]:
        with self._conn() as con:
            rows = con.execute("SELECT user_id FROM signups WHERE bingo_id=?;", (bingo_id,)).fetchall()
            return [int(r["user_id"]) for r in rows]

    # -------- teams --------
    def clear_teams(self, bingo_id: int) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM teams WHERE bingo_id=?;", (bingo_id,))

    def set_team(self, bingo_id: int, user_id: int, team_number: int) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO teams (bingo_id, team_number, user_id)
                VALUES (?, ?, ?)
                ON CONFLICT(bingo_id, user_id) DO UPDATE SET team_number=excluded.team_number;
                """,
                (bingo_id, team_number, user_id),
            )

    def get_team(self, bingo_id: int, user_id: int) -> Optional[int]:
        with self._conn() as con:
            row = con.execute("SELECT team_number FROM teams WHERE bingo_id=? AND user_id=?;", (bingo_id, user_id)).fetchone()
            return int(row["team_number"]) if row else None

    def list_teams(self, bingo_id: int) -> List[Tuple[int, int]]:
        with self._conn() as con:
            rows = con.execute("SELECT team_number, user_id FROM teams WHERE bingo_id=? ORDER BY team_number;", (bingo_id,)).fetchall()
            return [(int(r["team_number"]), int(r["user_id"])) for r in rows]

    # -------- timing --------
    def get_due_actions(self) -> List[sqlite3.Row]:
        now = utc_now().isoformat()
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT * FROM bingos
                WHERE status IN ('signup_open','signup_closed','running')
                ORDER BY id DESC;
                """
            ).fetchall()
            # Filtering in Python (small scale) keeps schema simple
            return rows

    # -------- submissions / approvals --------
    def create_submission(
        self,
        bingo_id: int,
        user_id: int,
        description: str,
        kind: str,
        message_id: int,
        attachment_path: str,
        status: str,
        approvals_required: int,
    ) -> int:
        with self._conn() as con:
            cur = con.execute(
                """
                INSERT INTO submissions (
                    bingo_id, user_id, description, kind, created_at_utc,
                    message_id, attachment_path, status, approvals_required
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    bingo_id,
                    user_id,
                    description,
                    kind,
                    utc_now().isoformat(),
                    message_id,
                    attachment_path,
                    status,
                    approvals_required,
                ),
            )
            return int(cur.lastrowid)

    def get_submission_by_message(self, message_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as con:
            return con.execute("SELECT * FROM submissions WHERE message_id=?;", (message_id,)).fetchone()

    def add_approval(self, submission_id: int, user_id: int) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO approvals (submission_id, user_id, approved_at_utc)
                VALUES (?, ?, ?);
                """,
                (submission_id, user_id, utc_now().isoformat()),
            )

    def count_approvals(self, submission_id: int) -> int:
        with self._conn() as con:
            row = con.execute("SELECT COUNT(*) AS c FROM approvals WHERE submission_id=?;", (submission_id,)).fetchone()
            return int(row["c"])

    def set_submission_status(self, submission_id: int, status: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE submissions SET status=? WHERE id=?;", (status, submission_id))

    def leaderboard_counts(self, bingo_id: int) -> List[Tuple[int, int]]:
        """
        Returns list of (team_number, approved_full_tile_count).
        """
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT t.team_number AS team_number, COUNT(s.id) AS cnt
                FROM submissions s
                JOIN teams t ON t.bingo_id = s.bingo_id AND t.user_id = s.user_id
                WHERE s.bingo_id=? AND s.status='approved' AND s.kind='full_tile'
                GROUP BY t.team_number
                ORDER BY cnt DESC, t.team_number ASC;
                """,
                (bingo_id,),
            ).fetchall()
            return [(int(r["team_number"]), int(r["cnt"])) for r in rows]


intents = discord.Intents.default()
intents.message_content = True  # required for prefix commands like !bingosetup / !bingosubmit
intents.members = True          # helpful for role checks
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
db = BingoDB(DB_PATH)


async def dm_ask(user: discord.abc.User, prompt: str, timeout: int = 300) -> Optional[discord.Message]:
    """
    DM the user a prompt, wait for their DM reply, return the message or None on timeout.
    """
    try:
        dm = user.dm_channel or await user.create_dm()
        await dm.send(prompt)
    except discord.Forbidden:
        return None

    def check(m: discord.Message) -> bool:
        return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)

    try:
        return await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        return None


def build_signup_embed(bingo: sqlite3.Row, gs: GuildSettings) -> discord.Embed:
    embed = discord.Embed(
        title=f"OSRS Bingo Signup: {bingo['name']}",
        description=f"Game mode: **{bingo['game_mode']}**\nReact with {SIGNUP_EMOJI} to sign up, {UNSIGN_EMOJI} to remove yourself.",
        timestamp=utc_now(),
    )
    embed.add_field(name="Start (UTC)", value=bingo["start_utc"], inline=False)
    embed.add_field(name="Signup closes (UTC)", value=bingo["signup_close_utc"], inline=False)
    embed.add_field(name="Team size", value=str(bingo["team_size"]), inline=True)
    embed.add_field(name="Team selection", value=bingo["team_mode"], inline=True)
    embed.add_field(name="Submissions channel", value=f"<#{gs.submissions_channel_id}>", inline=False)
    embed.set_footer(text="Bingo Bot")
    return embed


async def try_send_board(guild: discord.Guild, gs: GuildSettings, bingo: sqlite3.Row) -> None:
    path = bingo["board_image_path"]
    if not path:
        return
    ch = guild.get_channel(gs.board_channel_id)
    if not isinstance(ch, discord.TextChannel):
        return
    try:
        file = discord.File(path)
        await ch.send(content=f"**Bingo Board:** {bingo['name']}", file=file)
    except Exception as e:
        log.warning("Failed to send board image: %s", e)


async def update_leaderboard(guild: discord.Guild, gs: GuildSettings, bingo: sqlite3.Row) -> None:
    """
    Minimal placeholder leaderboard: shows approved full-tile submissions per team.
    """
    ann = guild.get_channel(gs.announcements_channel_id)
    if not isinstance(ann, discord.TextChannel):
        return

    counts = db.leaderboard_counts(bingo["id"])
    lines = []
    if not counts:
        lines.append("No approved full-tile submissions yet.")
    else:
        for team_num, cnt in counts:
            lines.append(f"Team {team_num}: **{cnt}**")

    content = f"**Leaderboard (placeholder): {bingo['name']}**\n" + "\n".join(lines)

    lb_id = bingo["leaderboard_message_id"]
    try:
        if lb_id:
            msg = await ann.fetch_message(lb_id)
            await msg.edit(content=content)
        else:
            msg = await ann.send(content)
            db.set_leaderboard_message_id(bingo["id"], msg.id)
    except Exception as e:
        log.warning("Leaderboard update failed: %s", e)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    if not bingo_tick.is_running():
        bingo_tick.start()


# ----------------------------
# Setup: !bingosetup (DM wizard)
# ----------------------------
@bot.command(name="bingosetup")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def bingosetup(ctx: commands.Context):
    active = db.get_active_bingo(ctx.guild.id)
    if active:
        await ctx.reply("There is already an active bingo on this server. End it (not implemented yet) or remove it from the DB.")
        return

    # DM wizard
    user = ctx.author
    m = await dm_ask(user, "Bingo setup started.\n\nFirst: mention the **signup channel** like `<#123>`.")
    if not m:
        await ctx.reply("I could not DM you (or you timed out). Please enable DMs from server members and try again.")
        return
    signup_ch = parse_channel_mention(m.content)
    if not signup_ch:
        await user.send("Invalid channel mention. Setup cancelled.")
        return

    m = await dm_ask(user, "Mention the **submissions channel** like `<#123>`.")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    submissions_ch = parse_channel_mention(m.content)
    if not submissions_ch:
        await user.send("Invalid channel mention. Setup cancelled.")
        return

    m = await dm_ask(user, "Mention the **announcements channel** like `<#123>`.")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    announcements_ch = parse_channel_mention(m.content)
    if not announcements_ch:
        await user.send("Invalid channel mention. Setup cancelled.")
        return

    m = await dm_ask(user, "Mention the **bingo-board channel** like `<#123>`.")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    board_ch = parse_channel_mention(m.content)
    if not board_ch:
        await user.send("Invalid channel mention. Setup cancelled.")
        return

    gs = GuildSettings(
        guild_id=ctx.guild.id,
        signup_channel_id=signup_ch,
        submissions_channel_id=submissions_ch,
        announcements_channel_id=announcements_ch,
        board_channel_id=board_ch,
    )
    db.upsert_guild_settings(gs)

    m = await dm_ask(user, "Bingo name? (type `none` to default to your name)")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    name = m.content.strip()
    if name.lower() == "none" or name == "":
        name = f"{user.display_name}'s bingo"

    m = await dm_ask(user, "Game mode? (example: `Jesus' custom bingo`)")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    game_mode = m.content.strip() or "Custom"

    m = await dm_ask(user, "Team size? (`0` = everyone on one team, `1` = solo, `2+` = teams)")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    team_size = parse_int(m.content, min_value=0, max_value=200)
    if team_size is None:
        await user.send("Invalid team size. Setup cancelled.")
        return

    m = await dm_ask(user, "Team selection mode: `random`, `captains`, or `preferred`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    team_mode = m.content.strip().lower()
    if team_mode not in ("random", "captains", "preferred"):
        await user.send("Invalid team mode. Setup cancelled.")
        return

    m = await dm_ask(user, "Start datetime in format `YYYY-MM-DD HH:MM` (example: `2026-01-17 19:00`)")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    start_str = m.content.strip()

    m = await dm_ask(user, "Timezone (IANA), example: `America/New_York` or `UTC`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    tz_str = m.content.strip()
    start_utc = parse_dt_with_tz(start_str, tz_str)
    if not start_utc:
        await user.send("Could not parse start datetime/timezone. Setup cancelled.")
        return

    m = await dm_ask(user, "End datetime (same format) or type `none`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    end_utc: Optional[datetime] = None
    if m.content.strip().lower() != "none":
        end_utc = parse_dt_with_tz(m.content.strip(), tz_str)
        if not end_utc:
            await user.send("Could not parse end datetime. Setup cancelled.")
            return

    m = await dm_ask(user, "When to close signups before start (hours). Example: `2`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    close_hours = parse_int(m.content, min_value=0, max_value=240)
    if close_hours is None:
        await user.send("Invalid number. Setup cancelled.")
        return
    signup_close_utc = start_utc - timedelta(hours=close_hours)  # noqa: F821

    m = await dm_ask(user, "Which role to notify? Mention like `<@&123>` or type `none`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    notify_role_id = parse_role_mention(m.content)
    if m.content.strip().lower() not in ("none", "no", "n/a") and notify_role_id is None:
        await user.send("Invalid role mention. Setup cancelled.")
        return

    m = await dm_ask(user, "When to show the bingo board? `signup_created`, `signups_close`, or `bingo_start`")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    show_board_when = m.content.strip().lower()
    if show_board_when not in ("signup_created", "signups_close", "bingo_start"):
        await user.send("Invalid choice. Setup cancelled.")
        return

    m = await dm_ask(user, "Submissions require approval? `none`, `admin`, or `nonteammate` (nonteammate not implemented yet)")
    if not m:
        await user.send("Timed out. Setup cancelled.")
        return
    approvals_mode = m.content.strip().lower()
    if approvals_mode not in ("none", "admin", "nonteammate"):
        await user.send("Invalid choice. Setup cancelled.")
        return

    approvals_required = 0
    if approvals_mode != "none":
        m = await dm_ask(user, "How many approvals required? Example: `1`")
        if not m:
            await user.send("Timed out. Setup cancelled.")
            return
        approvals_required = parse_int(m.content, min_value=1, max_value=20)
        if approvals_required is None:
            await user.send("Invalid number. Setup cancelled.")
            return

    # Optional board image upload in DM
    await user.send("Optional: upload the bingo board image now in this DM, or type `skip`.")
    board_image_path = None

    def dm_check(mm: discord.Message) -> bool:
        return mm.author.id == user.id and isinstance(mm.channel, discord.DMChannel)

    try:
        mm = await bot.wait_for("message", check=dm_check, timeout=300)
        if mm.content.strip().lower() != "skip" and mm.attachments:
            att = mm.attachments[0]
            ext = os.path.splitext(att.filename)[1].lower() or ".png"
            board_image_path = os.path.join(DATA_DIR, "boards", f"board_{ctx.guild.id}_{int(utc_now().timestamp())}{ext}")
            await att.save(board_image_path)
    except asyncio.TimeoutError:
        pass

    # Create bingo in DB
    bingo_id = db.create_bingo(
        guild_id=ctx.guild.id,
        name=name,
        game_mode=game_mode,
        team_size=team_size,
        team_mode=team_mode,
        notify_role_id=notify_role_id,
        start_utc=start_utc,
        end_utc=end_utc,
        signup_close_utc=signup_close_utc,
        show_board_when=show_board_when,
        approvals_mode=approvals_mode,
        approvals_required=approvals_required,
        created_by_user_id=user.id,
        board_image_path=board_image_path,
    )
    bingo = db.get_active_bingo(ctx.guild.id)

    # Create signup post
    signup_channel = ctx.guild.get_channel(gs.signup_channel_id)
    if not isinstance(signup_channel, discord.TextChannel):
        await user.send("Setup saved, but signup channel is not a text channel. Fix and rerun.")
        return

    notify_text = f"<@&{notify_role_id}> " if notify_role_id else ""
    embed = build_signup_embed(bingo, gs)
    msg = await signup_channel.send(content=f"{notify_text}Signups are open!", embed=embed)
    await msg.add_reaction(SIGNUP_EMOJI)
    await msg.add_reaction(UNSIGN_EMOJI)

    db.set_signup_message_id(bingo_id, msg.id)

    # Reveal board if configured at signup creation
    if show_board_when == "signup_created":
        await try_send_board(ctx.guild, gs, bingo)

    # Create / update leaderboard message
    await update_leaderboard(ctx.guild, gs, bingo)

    await ctx.reply("Setup complete. Check your signup channel.")


# ----------------------------
# Submissions: !bingosubmit and /bingosubmit
# ----------------------------
@bot.command(name="bingosubmit")
@commands.guild_only()
async def bingosubmit(ctx: commands.Context, *, description: str):
    await handle_submission(ctx.guild, ctx.author, ctx.channel, description, ctx.message.attachments)


async def handle_submission(
    guild: discord.Guild,
    author: discord.Member,
    channel: discord.abc.Messageable,
    description: str,
    attachments: List[discord.Attachment],
):
    bingo = db.get_active_bingo(guild.id)
    if not bingo:
        await channel.send("No active bingo found on this server.")
        return

    gs = db.get_guild_settings(guild.id)
    if not gs:
        await channel.send("Bingo is not configured. Run `!bingosetup`.")
        return

    # Enforce submissions channel
    if isinstance(channel, discord.TextChannel) and channel.id != gs.submissions_channel_id:
        await channel.send(f"Please submit in <#{gs.submissions_channel_id}>.")
        return

    if not attachments:
        await channel.send("Attach an image to your submission.")
        return

    # Ask if progress or full tile
    # (Base: ask via a quick follow-up message; you can convert to buttons later.)
    q = await channel.send("Is this `progress` or `full_tile`? Reply with one word.")
    kind = "progress"

    def check(m: discord.Message) -> bool:
        return m.author.id == author.id and m.channel == q.channel

    try:
        reply = await bot.wait_for("message", check=check, timeout=60)
        if reply.content.strip().lower() in ("full", "full_tile", "tile", "fulltile"):
            kind = "full_tile"
        else:
            kind = "progress"
    except asyncio.TimeoutError:
        kind = "progress"

    att = attachments[0]
    ext = os.path.splitext(att.filename)[1].lower() or ".png"
    local_path = os.path.join(DATA_DIR, "submissions", f"sub_{guild.id}_{author.id}_{int(utc_now().timestamp())}{ext}")
    await att.save(local_path)

    submissions_channel = guild.get_channel(gs.submissions_channel_id)
    if not isinstance(submissions_channel, discord.TextChannel):
        await channel.send("Submissions channel misconfigured.")
        return

    embed = discord.Embed(
        title="Bingo Submission",
        description=description,
        timestamp=utc_now(),
    )
    embed.add_field(name="Player", value=author.mention, inline=True)
    embed.add_field(name="Type", value=kind, inline=True)

    file = discord.File(local_path)
    embed.set_image(url=f"attachment://{os.path.basename(local_path)}")

    status = "approved" if bingo["approvals_mode"] == "none" else "pending"
    approvals_required = int(bingo["approvals_required"])

    msg = await submissions_channel.send(embed=embed, file=file)
    await msg.add_reaction(SIGNUP_EMOJI)  # receipt checkmark per your flow
    if status == "pending":
        await msg.add_reaction(APPROVE_EMOJI)
        await submissions_channel.send(f"{author.mention} submission received; awaiting approval.")

    submission_id = db.create_submission(
        bingo_id=bingo["id"],
        user_id=author.id,
        description=description,
        kind=kind,
        message_id=msg.id,
        attachment_path=local_path,
        status=status,
        approvals_required=approvals_required,
    )

    # If auto-approved, update leaderboard now
    if status == "approved":
        await update_leaderboard(guild, gs, bingo)

    log.info("Submission created: %s (db id %s)", msg.id, submission_id)


# ----------------------------
# Reactions: signup + approvals
# ----------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if not payload.guild_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    gs = db.get_guild_settings(guild.id)
    if not gs:
        return

    emoji = str(payload.emoji)
    bingo = db.get_active_bingo(guild.id)
    if not bingo:
        return

    # --- signup post reactions ---
    if bingo["signup_message_id"] and payload.message_id == int(bingo["signup_message_id"]):
        if emoji == SIGNUP_EMOJI:
            db.add_signup(bingo["id"], payload.user_id)
        elif emoji == UNSIGN_EMOJI:
            db.remove_signup(bingo["id"], payload.user_id)
        return

    # --- approvals on submission messages ---
    if emoji != APPROVE_EMOJI:
        return

    sub = db.get_submission_by_message(payload.message_id)
    if not sub:
        return
    if sub["status"] != "pending":
        return

    # Determine whether this approver is allowed
    approvals_mode = bingo["approvals_mode"]
    allowed = False

    try:
        member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    except Exception:
        member = None

    if approvals_mode == "admin":
        if member and (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
            allowed = True
    elif approvals_mode == "nonteammate":
        # Reserved for later: ensure approver is not on same team.
        # For now, treat as admin-only to avoid abuse.
        if member and (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
            allowed = True
    else:
        allowed = False

    if not allowed:
        return

    db.add_approval(sub["id"], payload.user_id)
    current = db.count_approvals(sub["id"])
    needed = int(sub["approvals_required"])

    if current >= needed:
        db.set_submission_status(sub["id"], "approved")

        ann = guild.get_channel(gs.announcements_channel_id)
        if isinstance(ann, discord.TextChannel):
            await ann.send(f"✅ Submission approved (message {sub['message_id']}).")

        # Refresh bingo row after possible edits
        bingo = db.get_active_bingo(guild.id)
        if bingo:
            await update_leaderboard(guild, gs, bingo)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    """
    Optional: removing ✅ from signup also removes signup.
    Keeping it simple; your explicit ❌ also works.
    """
    if payload.user_id == bot.user.id:
        return
    if not payload.guild_id:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    bingo = db.get_active_bingo(guild.id)
    if not bingo or not bingo["signup_message_id"]:
        return

    if payload.message_id == int(bingo["signup_message_id"]) and str(payload.emoji) == SIGNUP_EMOJI:
        db.remove_signup(bingo["id"], payload.user_id)


# ----------------------------
# Background tick: close signups, create teams, reveal board, start bingo
# ----------------------------
from datetime import timedelta  # placed here to avoid clutter above

@tasks.loop(seconds=30)
async def bingo_tick():
    for row in db.get_due_actions():
        try:
            await handle_bingo_state(row)
        except Exception as e:
            log.exception("bingo_tick error: %s", e)


async def handle_bingo_state(bingo: sqlite3.Row):
    guild = bot.get_guild(int(bingo["guild_id"]))
    if not guild:
        return
    gs = db.get_guild_settings(guild.id)
    if not gs:
        return

    now = utc_now()
    start_utc = datetime.fromisoformat(bingo["start_utc"])
    signup_close_utc = datetime.fromisoformat(bingo["signup_close_utc"])
    status = bingo["status"]

    # Close signups and make teams
    if status == "signup_open" and now >= signup_close_utc:
        db.set_status(bingo["id"], "signup_closed")

        # Create teams (random only, for now)
        signups = db.list_signups(bingo["id"])
        random.shuffle(signups)

        team_size = int(bingo["team_size"])
        db.clear_teams(bingo["id"])

        if team_size <= 0:
            # One team for everyone
            for uid in signups:
                db.set_team(bingo["id"], uid, 1)
        elif team_size == 1:
            # Solo teams
            for idx, uid in enumerate(signups, start=1):
                db.set_team(bingo["id"], uid, idx)
        else:
            team_num = 1
            for i, uid in enumerate(signups):
                db.set_team(bingo["id"], uid, team_num)
                if (i + 1) % team_size == 0:
                    team_num += 1

        # Announce teams
        ann = guild.get_channel(gs.announcements_channel_id)
        if isinstance(ann, discord.TextChannel):
            teams = db.list_teams(bingo["id"])
            bucket = {}
            for tn, uid in teams:
                bucket.setdefault(tn, []).append(uid)

            lines = []
            for tn in sorted(bucket.keys()):
                mentions = " ".join(f"<@{u}>" for u in bucket[tn])
                lines.append(f"**Team {tn}:** {mentions}")

            await ann.send(f"Signups closed for **{bingo['name']}**.\n" + "\n".join(lines))

        # Reveal board when signups close
        if bingo["show_board_when"] == "signups_close":
            await try_send_board(guild, gs, bingo)

        # Update leaderboard (teams may now exist)
        await update_leaderboard(guild, gs, bingo)

    # Start bingo
    if status in ("signup_open", "signup_closed") and now >= start_utc:
        db.set_status(bingo["id"], "running")
        ann = guild.get_channel(gs.announcements_channel_id)
        if isinstance(ann, discord.TextChannel):
            await ann.send(f"🚀 **{bingo['name']}** has started!")

        if bingo["show_board_when"] == "bingo_start":
            await try_send_board(guild, gs, bingo)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Put it in .env")

    bot.run(TOKEN)
