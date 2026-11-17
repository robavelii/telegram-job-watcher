"""SQLite state: seen posts, scoring results, the quiet-hours outbox, and small metadata."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

# A vacancy cross-posted to several channels usually differs only in links, @mentions,
# hashtags, emoji and the channel's footer, so those are stripped before hashing.
_URL = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)
_TAG = re.compile(r"[@#]\w+")
_NON_WORD = re.compile(r"[^\w]+")

DUPLICATE_WINDOW_SECONDS = 14 * 24 * 3600
# Share of the shorter post's 3-word shingles found in the other post. Footers, hashtags
# and "subscribe" lines move this a little; a different vacancy moves it a lot.
NEAR_DUPLICATE_CONTAINMENT = 0.8
NEAR_DUPLICATE_SCAN_LIMIT = 3000

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    text_hash  TEXT    NOT NULL,
    norm_text  TEXT    NOT NULL DEFAULT '',
    seen_at    REAL    NOT NULL,
    duplicate  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS posts_hash ON posts (text_hash, seen_at);

CREATE TABLE IF NOT EXISTS results (
    chat_id      INTEGER NOT NULL,
    msg_id       INTEGER NOT NULL,
    link         TEXT    NOT NULL,
    channel      TEXT    NOT NULL,
    status       TEXT    NOT NULL,   -- scored | error
    is_job       INTEGER,
    fit_score    INTEGER,
    eligible     TEXT,
    result_json  TEXT,
    error        TEXT,
    created_at   REAL    NOT NULL,
    alerted_at   REAL,
    digested_at  REAL,
    PRIMARY KEY (chat_id, msg_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    sent_at     REAL
);

-- One row per post the pipeline looked at, for summaries (skips included).
CREATE TABLE IF NOT EXISTS outcomes (
    ts       REAL NOT NULL,
    outcome  TEXT NOT NULL,
    chat_id  INTEGER,
    msg_id   INTEGER,
    link     TEXT NOT NULL DEFAULT '',
    channel  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS outcomes_ts ON outcomes (ts);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


def normalize(text: str) -> str:
    text = text.lower()
    text = _URL.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def shingles(normalized: str, size: int = 3) -> set[tuple[str, ...]]:
    words = normalized.split()
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def containment(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


class Store:
    def __init__(self, path: Path | str, clock=time.time):
        self._clock = clock
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that later versions introduced to tables an older version created."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(outcomes)")}
        for column, ddl in (
            ("chat_id", "INTEGER"),
            ("msg_id", "INTEGER"),
            ("link", "TEXT NOT NULL DEFAULT ''"),
            ("channel", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in have:
                self.db.execute(f"ALTER TABLE outcomes ADD COLUMN {column} {ddl}")
        self.db.commit()

    # --- dedupe -----------------------------------------------------------------

    def claim(self, chat_id: int, msg_id: int, text: str) -> str:
        """Record a post. Returns 'new', 'seen' (same message again) or 'duplicate'
        (the same vacancy in another message within the duplicate window, exact or near).

        No await between the checks and the insert, so this is atomic under asyncio."""
        now = self._clock()
        norm = normalize(text)
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        if self.db.execute(
            "SELECT 1 FROM posts WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        ).fetchone():
            return "seen"
        since = now - DUPLICATE_WINDOW_SECONDS
        twin = self.db.execute(
            "SELECT 1 FROM posts WHERE text_hash = ? AND seen_at > ? LIMIT 1", (digest, since)
        ).fetchone() is not None
        if not twin:
            mine = shingles(norm)
            recent = self.db.execute(
                "SELECT norm_text FROM posts WHERE seen_at > ? ORDER BY seen_at DESC LIMIT ?",
                (since, NEAR_DUPLICATE_SCAN_LIMIT),
            )
            twin = any(
                containment(mine, shingles(row["norm_text"])) >= NEAR_DUPLICATE_CONTAINMENT for row in recent
            )
        with self.db:
            self.db.execute(
                """INSERT INTO posts (chat_id, msg_id, text_hash, norm_text, seen_at, duplicate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chat_id, msg_id, digest, norm, now, 1 if twin else 0),
            )
        return "duplicate" if twin else "new"

    def last_msg_id(self, chat_id: int) -> int | None:
        row = self.db.execute("SELECT MAX(msg_id) AS m FROM posts WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["m"] if row and row["m"] is not None else None

    # --- results ----------------------------------------------------------------

    def record_result(self, chat_id: int, msg_id: int, link: str, channel: str, result: dict) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO results
                   (chat_id, msg_id, link, channel, status, is_job, fit_score, eligible, result_json, created_at)
                   VALUES (?, ?, ?, ?, 'scored', ?, ?, ?, ?, ?)""",
                (
                    chat_id,
                    msg_id,
                    link,
                    channel,
                    1 if result.get("is_job_posting") else 0,
                    int(result.get("fit_score", 0)),
                    result.get("eligible_from_ethiopia", "unclear"),
                    json.dumps(result, ensure_ascii=False),
                    self._clock(),
                ),
            )

    def record_error(self, chat_id: int, msg_id: int, link: str, channel: str, error: str) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO results (chat_id, msg_id, link, channel, status, error, created_at)
                   VALUES (?, ?, ?, ?, 'error', ?, ?)""",
                (chat_id, msg_id, link, channel, error[:500], self._clock()),
            )

    def mark_alerted(self, chat_id: int, msg_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE results SET alerted_at = ? WHERE chat_id = ? AND msg_id = ?",
                (self._clock(), chat_id, msg_id),
            )

    # --- summaries --------------------------------------------------------------

    def record_outcome(
        self, outcome: str, chat_id: int | None = None, msg_id: int | None = None, link: str = "", channel: str = ""
    ) -> None:
        now = self._clock()
        with self.db:
            self.db.execute(
                "INSERT INTO outcomes (ts, outcome, chat_id, msg_id, link, channel) VALUES (?, ?, ?, ?, ?, ?)",
                (now, outcome, chat_id, msg_id, link, channel),
            )
            self.db.execute("DELETE FROM outcomes WHERE ts < ?", (now - 30 * 24 * 3600,))

    def outcome_counts(self, since: float) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT outcome, COUNT(*) AS n FROM outcomes WHERE ts >= ? GROUP BY outcome", (since,)
        ).fetchall()
        return {r["outcome"]: r["n"] for r in rows}

    def outcomes_since(self, since: float) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outcomes WHERE ts >= ? ORDER BY ts", (since,)).fetchall()

    def results_since(self, since: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM results WHERE created_at >= ? ORDER BY fit_score DESC, created_at", (since,)
        ).fetchall()

    def last_outcome_at(self) -> float | None:
        row = self.db.execute("SELECT MAX(ts) AS t FROM outcomes").fetchone()
        return row["t"] if row else None

    # --- outbox (quiet hours) ---------------------------------------------------

    def enqueue(self, body: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO outbox (body, created_at) VALUES (?, ?)", (body, self._clock()))

    def pending_outbox(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall()

    def mark_sent(self, outbox_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (self._clock(), outbox_id))

    # --- meta -------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
