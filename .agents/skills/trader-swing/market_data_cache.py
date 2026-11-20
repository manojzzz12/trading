"""Local SQLite cache for normalized market data in trader-swing.

Features:
1. Thread-safe, transaction-isolated local storage for historical and current candles.
2. ACID integrity preventing database corruption during interrupted writes.
3. Indexed lookup on (ticker, interval, timestamp) preventing duplicate bars.
4. Updates forming bars cleanly when completed bars become available.
5. Querying historical range and latest available candle.
6. Cache metadata tracking (last_updated, last_status, staleness checks).
7. Pure local operation with zero credential persistence.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

# Ensure local imports work regardless of execution context
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from market_data_models import (
    CandleStatus,
    OHLCVCandle,
    parse_iso_timestamp,
    to_utc,
    UTC,
)

DEFAULT_CACHE_DIR = os.path.join(CURRENT_DIR, "data")
DEFAULT_CACHE_DB = os.path.join(DEFAULT_CACHE_DIR, "market_data.db")


class MarketDataCache:
    """Persistent local cache for OHLCV candles with duplicate prevention and ACID writes."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_CACHE_DB
        self._is_memory = (self.db_path == ":memory:")

        if not self._is_memory:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        """Create candle and metadata tables if they do not exist."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    ticker TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    provider_event_timestamp TEXT,
                    received_timestamp TEXT NOT NULL,
                    processing_timestamp TEXT,
                    is_delayed INTEGER NOT NULL,
                    bar_end_timestamp TEXT,
                    PRIMARY KEY (ticker, interval, timestamp)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_lookup 
                ON candles (ticker, interval, timestamp)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_meta (
                    ticker TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    last_status TEXT NOT NULL,
                    PRIMARY KEY (ticker, interval)
                )
                """
            )

    def store_candles(self, candles: List[OHLCVCandle], now: Optional[datetime] = None) -> int:
        """Store or update candles inside an atomic transaction. Returns count of stored candles."""
        if not candles:
            return 0

        now_utc = to_utc(now) if now is not None else (candles[-1].received_timestamp if candles else datetime.now(UTC))
        now_iso = now_utc.isoformat()
        records = []
        meta_updates = set()

        for c in candles:
            records.append((
                c.ticker,
                c.interval,
                c.timestamp.isoformat(),
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                c.volume,
                c.status.value,
                c.source,
                c.provider_event_timestamp.isoformat() if c.provider_event_timestamp else None,
                c.received_timestamp.isoformat(),
                c.processing_timestamp.isoformat() if c.processing_timestamp else None,
                1 if c.is_delayed else 0,
                c.bar_end_timestamp.isoformat() if c.bar_end_timestamp else None,
            ))
            meta_updates.add((c.ticker, c.interval, c.status.value))

        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO candles (
                    ticker, interval, timestamp, open, high, low, close, volume,
                    status, source, provider_event_timestamp, received_timestamp,
                    processing_timestamp, is_delayed, bar_end_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, interval, timestamp) DO UPDATE SET
                    open = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.open
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.open
                        ELSE excluded.open
                    END,
                    high = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.high
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.high
                        ELSE excluded.high
                    END,
                    low = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.low
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.low
                        ELSE excluded.low
                    END,
                    close = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.close
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.close
                        ELSE excluded.close
                    END,
                    volume = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.volume
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.volume
                        ELSE excluded.volume
                    END,
                    status = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.status
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.status
                        ELSE excluded.status
                    END,
                    source = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.source
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.source
                        ELSE excluded.source
                    END,
                    provider_event_timestamp = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.provider_event_timestamp
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.provider_event_timestamp
                        ELSE excluded.provider_event_timestamp
                    END,
                    received_timestamp = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.received_timestamp
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.received_timestamp
                        ELSE excluded.received_timestamp
                    END,
                    processing_timestamp = excluded.processing_timestamp,
                    is_delayed = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.is_delayed
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.is_delayed
                        ELSE excluded.is_delayed
                    END,
                    bar_end_timestamp = CASE
                        WHEN candles.status = 'COMPLETE' AND excluded.status IN ('FORMING', 'UNKNOWN') THEN candles.bar_end_timestamp
                        WHEN candles.status = 'FORMING' AND excluded.status = 'UNKNOWN' THEN candles.bar_end_timestamp
                        ELSE excluded.bar_end_timestamp
                    END
                """,
                records,
            )

            for ticker, interval, status_val in meta_updates:
                self._conn.execute(
                    """
                    INSERT INTO cache_meta (ticker, interval, last_updated, last_status)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(ticker, interval) DO UPDATE SET
                        last_updated = excluded.last_updated,
                        last_status = CASE
                            WHEN cache_meta.last_status = 'COMPLETE' AND excluded.last_status IN ('FORMING', 'UNKNOWN') THEN cache_meta.last_status
                            WHEN cache_meta.last_status = 'FORMING' AND excluded.last_status = 'UNKNOWN' THEN cache_meta.last_status
                            ELSE excluded.last_status
                        END
                    """,
                    (ticker, interval, now_iso, status_val),
                )

        return len(records)

    def get_historical_candles(
        self,
        ticker: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> List[OHLCVCandle]:
        """Retrieve stored candles in [start, end] ordered chronologically."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()
        start_iso = to_utc(start).isoformat()
        end_iso = to_utc(end).isoformat()

        cursor = self._conn.execute(
            """
            SELECT * FROM candles
            WHERE ticker = ? AND interval = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (sym, inv, start_iso, end_iso),
        )

        rows = cursor.fetchall()
        return [self._row_to_candle(r) for r in rows]

    def get_latest_candle(
        self,
        ticker: str,
        interval: str,
    ) -> Optional[OHLCVCandle]:
        """Retrieve the most recent candle for the specified ticker and interval."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()

        cursor = self._conn.execute(
            """
            SELECT * FROM candles
            WHERE ticker = ? AND interval = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (sym, inv),
        )
        row = cursor.fetchone()
        return self._row_to_candle(row) if row else None

    def is_stale(
        self,
        ticker: str,
        interval: str,
        max_age_seconds: int,
        current_time: Optional[datetime] = None,
    ) -> bool:
        """Check whether cached metadata for ticker/interval is older than max_age_seconds."""
        sym = ticker.upper().strip()
        inv = interval.lower().strip()

        cursor = self._conn.execute(
            """
            SELECT last_updated FROM cache_meta
            WHERE ticker = ? AND interval = ?
            """,
            (sym, inv),
        )
        row = cursor.fetchone()
        if not row:
            return True

        last_updated = parse_iso_timestamp(row["last_updated"])
        now_utc = to_utc(current_time) if current_time is not None else datetime.now(UTC)
        age = (now_utc - last_updated).total_seconds()
        return age > max_age_seconds

    def clear(self, ticker: Optional[str] = None, interval: Optional[str] = None) -> None:
        """Clear cached records for a specific ticker/interval or the entire database."""
        with self._conn:
            if ticker and interval:
                sym = ticker.upper().strip()
                inv = interval.lower().strip()
                self._conn.execute("DELETE FROM candles WHERE ticker = ? AND interval = ?", (sym, inv))
                self._conn.execute("DELETE FROM cache_meta WHERE ticker = ? AND interval = ?", (sym, inv))
            elif ticker:
                sym = ticker.upper().strip()
                self._conn.execute("DELETE FROM candles WHERE ticker = ?", (sym,))
                self._conn.execute("DELETE FROM cache_meta WHERE ticker = ?", (sym,))
            else:
                self._conn.execute("DELETE FROM candles")
                self._conn.execute("DELETE FROM cache_meta")

    def close(self) -> None:
        """Close database connection."""
        self._conn.close()

    def _row_to_candle(self, r: sqlite3.Row) -> OHLCVCandle:
        """Convert SQLite row to OHLCVCandle instance."""
        return OHLCVCandle(
            ticker=r["ticker"],
            interval=r["interval"],
            timestamp=parse_iso_timestamp(r["timestamp"]),
            open=Decimal(r["open"]),
            high=Decimal(r["high"]),
            low=Decimal(r["low"]),
            close=Decimal(r["close"]),
            volume=int(r["volume"]),
            status=CandleStatus(r["status"]),
            source=r["source"],
            provider_event_timestamp=parse_iso_timestamp(r["provider_event_timestamp"]) if r["provider_event_timestamp"] else None,
            received_timestamp=parse_iso_timestamp(r["received_timestamp"]),
            processing_timestamp=parse_iso_timestamp(r["processing_timestamp"]) if r["processing_timestamp"] else None,
            is_delayed=bool(r["is_delayed"]),
            bar_end_timestamp=parse_iso_timestamp(r["bar_end_timestamp"]) if r["bar_end_timestamp"] else None,
        )
