from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Quote, Tier

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY,
    origin        TEXT    NOT NULL,
    destination   TEXT    NOT NULL,
    trip_length   INTEGER,
    depart_date   TEXT    NOT NULL,
    return_date   TEXT,
    depart_month  TEXT    NOT NULL,
    price_inr     INTEGER NOT NULL,
    stops         INTEGER,
    airline       TEXT,
    distance_km   INTEGER,
    observed_at   TEXT    NOT NULL,
    observed_date TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_dedup
    ON observations(
        origin, destination, depart_date, COALESCE(return_date, ''), price_inr, observed_date
    );
CREATE INDEX IF NOT EXISTS idx_obs_route
    ON observations(origin, destination, trip_length);
CREATE INDEX IF NOT EXISTS idx_obs_seen ON observations(observed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    origin      TEXT    NOT NULL,
    destination TEXT    NOT NULL,
    trip_length INTEGER,
    depart_date TEXT    NOT NULL,
    price_inr   INTEGER NOT NULL,
    tier        TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_key
    ON alerts(origin, destination, trip_length, depart_date, sent_at);

CREATE TABLE IF NOT EXISTS discovered (
    destination TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    last_score  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

HISTORY_DAYS = 90


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- observations ----------------------------------------------------

    def record(self, quotes: Iterable[Quote]) -> int:
        """Insert new observations, ignoring ones already seen today.

        Dedup keys on the calendar day, not the exact timestamp: two provider
        calls in the same poll stamp microsecond-different `observed_at`
        values, so keying on that directly would never actually catch a
        duplicate. Same route/date/price seen again later the same day is
        treated as the same observation; a genuinely new price, or the same
        price on a later day, is not.
        """
        rows = []
        for q in quotes:
            observed_dt = q.observed_at or _now()
            observed = observed_dt.isoformat()
            rows.append(
                (
                    q.origin,
                    q.destination,
                    q.trip_length,
                    q.depart_date.isoformat(),
                    q.return_date.isoformat() if q.return_date else None,
                    q.depart_month,
                    q.price_inr,
                    q.stops,
                    q.airline,
                    q.distance_km,
                    observed,
                    observed_dt.date().isoformat(),
                )
            )
        if not rows:
            return 0
        cur = self.conn.executemany(
            """INSERT OR IGNORE INTO observations
               (origin, destination, trip_length, depart_date, return_date,
                depart_month, price_inr, stops, airline, distance_km, observed_at, observed_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return cur.rowcount

    def route_history(
        self,
        origin: str,
        destination: str,
        trip_length: int | None,
        *,
        days: int = HISTORY_DAYS,
    ) -> list[float]:
        """Prices seen on this exact route shape, for the baseline."""
        since = (_now() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT price_inr FROM observations
               WHERE origin=? AND destination=? AND trip_length IS ? AND observed_at >= ?""",
            (origin, destination, trip_length, since),
        ).fetchall()
        return [float(r["price_inr"]) for r in rows]

    def global_inr_per_km(self, *, days: int = HISTORY_DAYS, limit: int = 5_000) -> list[float]:
        """Cross-route price-per-km sample, used to judge routes with no history."""
        since = (_now() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            """SELECT price_inr, distance_km, trip_length FROM observations
               WHERE distance_km IS NOT NULL AND distance_km > 0 AND observed_at >= ?
               ORDER BY observed_at DESC LIMIT ?""",
            (since, limit),
        ).fetchall()
        out = []
        for r in rows:
            km = r["distance_km"] * (2 if r["trip_length"] is not None else 1)
            out.append(r["price_inr"] / km)
        return out

    def prune(self, *, days: int = HISTORY_DAYS) -> int:
        cutoff = (_now() - timedelta(days=days)).isoformat()
        cur = self.conn.execute("DELETE FROM observations WHERE observed_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM alerts WHERE sent_at < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def observation_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]

    # -- alerts ----------------------------------------------------------

    def should_alert(
        self,
        origin: str,
        destination: str,
        trip_length: int | None,
        depart_date: str,
        price_inr: int,
        tier: Tier,
        *,
        cooldown_hours: int = 48,
        redrop_pct: float = 10.0,
    ) -> bool:
        """Suppress repeats, but let a real improvement through.

        A price that fell another 10%, or a deal that climbed a tier, is new
        information and is allowed to interrupt even inside the cooldown.
        """
        since = (_now() - timedelta(hours=cooldown_hours)).isoformat()
        row = self.conn.execute(
            """SELECT price_inr, tier FROM alerts
               WHERE origin=? AND destination=? AND depart_date=?
                 AND (trip_length IS ?) AND sent_at >= ?
               ORDER BY sent_at DESC LIMIT 1""",
            (origin, destination, depart_date, trip_length, since),
        ).fetchone()
        if row is None:
            return True
        if price_inr <= row["price_inr"] * (1 - redrop_pct / 100.0):
            return True
        return tier.rank > Tier(row["tier"]).rank

    def record_alert(
        self,
        origin: str,
        destination: str,
        trip_length: int | None,
        depart_date: str,
        price_inr: int,
        tier: Tier,
    ) -> None:
        self.conn.execute(
            """INSERT INTO alerts
               (origin, destination, trip_length, depart_date, price_inr, tier, sent_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                origin,
                destination,
                trip_length,
                depart_date,
                price_inr,
                tier.value,
                _now().isoformat(),
            ),
        )
        self.conn.commit()

    # -- discovery -------------------------------------------------------

    def promote(self, destination: str, hotness: int) -> None:
        """Remember a destination the discovery arm found interesting."""
        now = _now().isoformat()
        self.conn.execute(
            """INSERT INTO discovered (destination, first_seen, last_seen, last_score)
               VALUES (?,?,?,?)
               ON CONFLICT(destination) DO UPDATE SET last_seen=?, last_score=?""",
            (destination, now, now, hotness, now, hotness),
        )
        self.conn.commit()

    def promoted(self, *, limit: int = 25) -> list[str]:
        rows = self.conn.execute(
            "SELECT destination FROM discovered ORDER BY last_score DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["destination"] for r in rows]

    # -- meta ------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO meta (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=?""",
            (key, value, value),
        )
        self.conn.commit()

    def recent_quotes(self, *, limit: int = 200) -> Sequence[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM observations ORDER BY observed_at DESC, price_inr ASC LIMIT ?""",
            (limit,),
        ).fetchall()
