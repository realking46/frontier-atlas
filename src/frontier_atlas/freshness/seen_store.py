"""Exactly-once processing across distributed crawler nodes.

Three layers of de-duplication:

1. **Canonical URL key** – tracking params, fragments, AMP variants, `www.`,
   trailing slashes and param order are normalised away, then hashed. The
   same article reached via a newsletter link and via the homepage gets one key.

2. **Atomic claim with lease** – before a node fetches/extracts an item it
   calls `claim(key, node_id)`. Exactly one caller wins (INSERT … ON CONFLICT
   DO NOTHING semantics). A claim is a *lease*: if the node dies mid-item, the
   lease expires and another node may re-claim it; `mark_done` makes it final.
   Production mapping: Redis `SET key node NX EX lease` / Postgres
   `INSERT … ON CONFLICT DO UPDATE … WHERE lease_until < now() RETURNING`.

3. **Content SimHash** – syndicated copies (same press release on 5 sites,
   same job on 3 boards) have different URLs but near-identical text. A 64-bit
   SimHash split into 4×16-bit bands gives an index where any pair within
   Hamming distance ≤ 3 shares at least one band (pigeonhole), so lookups are
   O(candidates), not O(corpus).

Also keeps per-source run history and "observed" URLs, which the no-date
heuristics use to decide what is new since the last run.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_hsenc|_hsmi|mkt_tok|ref|ref_src|ref_url|"
    r"source|src|cmpid|campaign|igshid|si|s_cid|ncid|guccounter|guce_\w+|trk|trkInfo|originalSubdomain|"
    r"amp|outputType|output|share|via|feature|spm)$",
    re.I,
)
AMP_PATH_RE = re.compile(r"(/amp/?$|/amp/(?=.)|\.amp$|\.amp(?=\.html$))", re.I)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme.lower()
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = AMP_PATH_RE.sub("/", parts.path or "/")
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                             if not TRACKING_PARAMS.match(k)))
    return urlunsplit((scheme, host, path, query, ""))


def url_key(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()


# ---------------------------------------------------------------- SimHash
_WORD_RE = re.compile(r"[a-z0-9]+")


def simhash(text: str, shingle: int = 3) -> int:
    words = _WORD_RE.findall(text.lower())
    if len(words) < shingle:
        words = words + [""] * (shingle - len(words))
    v = [0] * 64
    for i in range(len(words) - shingle + 1):
        h = int.from_bytes(hashlib.blake2b(" ".join(words[i : i + shingle]).encode(), digest_size=8).digest(), "big")
        for b in range(64):
            v[b] += 1 if h >> b & 1 else -1
    return sum(1 << b for b in range(64) if v[b] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _bands(h: int) -> list[int]:
    return [(h >> (16 * i)) & 0xFFFF for i in range(4)]


def _to_sql_int(h: int) -> int:  # SQLite INTEGER is signed 64-bit
    return h - (1 << 64) if h >= 1 << 63 else h


def _from_sql_int(h: int) -> int:
    return h + (1 << 64) if h < 0 else h


# ---------------------------------------------------------------- store
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,
    status        TEXT NOT NULL,          -- claimed | done | rejected
    node          TEXT,
    lease_until   REAL,
    simhash       INTEGER,
    published_at  TEXT,
    reason        TEXT,
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS simhash_bands (
    band  INTEGER NOT NULL,
    value INTEGER NOT NULL,
    key   TEXT NOT NULL,
    PRIMARY KEY (band, value, key)
);
CREATE TABLE IF NOT EXISTS observed (
    source      TEXT NOT NULL,
    key         TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    first_run   INTEGER,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source, ok, started_at);
"""


@dataclass(slots=True)
class RunInfo:
    run_id: int
    source: str
    started_at: datetime


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


class SeenStore:
    def __init__(self, path: str | Path, *, lease_seconds: float = 600):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        try:  # migration for stores created before the label column existed
            self._conn.execute("ALTER TABLE items ADD COLUMN label TEXT")
        except sqlite3.OperationalError:
            pass
        self._lock = threading.Lock()
        self.lease_seconds = lease_seconds

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ claims
    def claim(self, key: str, *, source: str, url: str, node: str = "node-0",
              now: float | None = None) -> bool:
        """True if this caller now owns the item. False if it is done, rejected,
        or leased by another live node."""
        now = time.time() if now is None else now
        ts = _iso()
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO items(key, source, url, status, node, lease_until, first_seen, updated_at)
                   VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET node=excluded.node, lease_until=excluded.lease_until,
                                                  updated_at=excluded.updated_at
                   WHERE items.status='claimed' AND items.lease_until < ?""",
                (key, source, url, node, now + self.lease_seconds, ts, ts, now),
            )
            return cur.rowcount == 1

    def mark_done(self, key: str, *, simhash_value: int | None = None,
                  published_at: datetime | None = None, label: str | None = None) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "UPDATE items SET status='done', lease_until=NULL, simhash=?, published_at=?, label=?, updated_at=? "
                "WHERE key=?",
                (_to_sql_int(simhash_value) if simhash_value is not None else None,
                 _iso(published_at) if published_at else None, label, _iso(), key),
            )
            if simhash_value is not None:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO simhash_bands(band, value, key) VALUES (?, ?, ?)",
                    [(i, b, key) for i, b in enumerate(_bands(simhash_value))],
                )
            self._conn.execute("COMMIT")

    def mark_rejected(self, key: str, reason: str) -> None:
        """Final 'no' (stale, duplicate). Stops every node from re-checking it."""
        with self._lock:
            self._conn.execute(
                "UPDATE items SET status='rejected', lease_until=NULL, reason=?, updated_at=? WHERE key=?",
                (reason, _iso(), key),
            )

    def release(self, key: str) -> None:
        """Give the item back (e.g. transient fetch error) so it can be retried."""
        with self._lock:
            self._conn.execute("DELETE FROM items WHERE key=? AND status='claimed'", (key,))

    def status(self, key: str) -> str | None:
        row = self._conn.execute("SELECT status FROM items WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ near-duplicates
    def find_near_duplicate(
        self,
        h: int,
        *,
        max_distance: int = 3,
        label: str | None = None,
        min_label_ratio: int = 92,
    ) -> str | None:
        """Closest stored item within `max_distance` bits. If `label` is given (job
        title), the stored item's label must also match – templated descriptions
        shared by *different* roles at one company are not duplicates."""
        from rapidfuzz import fuzz

        bands = _bands(h)
        rows = self._conn.execute(
            "SELECT DISTINCT b.key, i.simhash, i.label FROM simhash_bands b JOIN items i ON i.key=b.key "
            "WHERE i.status='done' AND (" + " OR ".join(["(b.band=? AND b.value=?)"] * 4) + ")",
            [x for i, b in enumerate(bands) for x in (i, b)],
        ).fetchall()
        norm = _norm_label(label) if label else None
        best = None
        for key, sh, other_label in rows:
            if sh is None:
                continue
            d = hamming(h, _from_sql_int(sh))
            if d > max_distance:
                continue
            if norm is not None and other_label is not None and fuzz.ratio(norm, _norm_label(other_label)) < min_label_ratio:
                continue
            if best is None or d < best[1]:
                best = (key, d)
        return best[0] if best else None

    # ------------------------------------------------------------ runs + observations
    def start_run(self, source: str, now: datetime | None = None) -> RunInfo:
        started = now or datetime.now(timezone.utc)
        with self._lock:
            cur = self._conn.execute("INSERT INTO runs(source, started_at) VALUES (?, ?)", (source, _iso(started)))
        return RunInfo(cur.lastrowid, source, started)

    def finish_run(self, run: RunInfo, ok: bool = True) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET finished_at=?, ok=? WHERE run_id=?", (_iso(), int(ok), run.run_id))

    def last_successful_run(self, source: str, before_run: int | None = None) -> RunInfo | None:
        q = "SELECT run_id, started_at FROM runs WHERE source=? AND ok=1"
        args: list = [source]
        if before_run is not None:
            q += " AND run_id < ?"
            args.append(before_run)
        row = self._conn.execute(q + " ORDER BY run_id DESC LIMIT 1", args).fetchone()
        return RunInfo(row[0], source, datetime.fromisoformat(row[1])) if row else None

    def observe(self, source: str, keys: list[str], run: RunInfo) -> set[str]:
        """Record keys seen on a listing; returns the subset never observed before."""
        ts = _iso(run.started_at)
        with self._lock:
            known = {
                k for (k,) in self._conn.execute(
                    f"SELECT key FROM observed WHERE source=? AND key IN ({','.join('?' * len(keys))})",
                    [source, *keys],
                )
            } if keys else set()
            self._conn.execute("BEGIN")
            self._conn.executemany(
                "INSERT INTO observed(source, key, first_seen, last_seen, first_run) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source, key) DO UPDATE SET last_seen=excluded.last_seen",
                [(source, k, ts, ts, run.run_id) for k in keys],
            )
            self._conn.execute("COMMIT")
        return {k for k in keys if k not in known}

    def first_seen(self, source: str, key: str) -> datetime | None:
        row = self._conn.execute("SELECT first_seen FROM observed WHERE source=? AND key=?", (source, key)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None
