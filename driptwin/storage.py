"""Infusion log. Tiger Data (TimescaleDB) when DATABASE_URL is set, SQLite otherwise.

Telemetry goes into a hypertable so the dashboard and post-hoc review can ask
time-series questions ("rate per minute over the last hour") cheaply."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA_PG = [
    """CREATE TABLE IF NOT EXISTS telemetry (
        time TIMESTAMPTZ NOT NULL, infusion_id TEXT, rate_gtt_min REAL, target_gtt_min REAL,
        valve_u REAL, head_cm REAL, infused_ml REAL, mode TEXT)""",
    "SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE)",
    """CREATE TABLE IF NOT EXISTS events (
        time TIMESTAMPTZ NOT NULL, infusion_id TEXT, kind TEXT, severity TEXT,
        message TEXT, data JSONB)""",
]
SCHEMA_SQLITE = [
    """CREATE TABLE IF NOT EXISTS telemetry (time REAL, infusion_id TEXT, rate_gtt_min REAL,
        target_gtt_min REAL, valve_u REAL, head_cm REAL, infused_ml REAL, mode TEXT)""",
    """CREATE TABLE IF NOT EXISTS events (time REAL, infusion_id TEXT, kind TEXT,
        severity TEXT, message TEXT, data TEXT)""",
]


class Store:
    def __init__(self, url: str | None = None, prefix: str = "inf"):
        self.url = url or os.environ.get("DATABASE_URL")
        # "sim-..." for simulator runs, so practice data is never mistaken for a real infusion
        self.infusion_id = time.strftime(f"{prefix}-%Y%m%d-%H%M%S")
        if self.url:
            import psycopg
            self.conn = psycopg.connect(self.url, autocommit=True)
            self.kind = "timescale"
            for q in SCHEMA_PG:
                self.conn.execute(q)
        else:
            path = Path(__file__).resolve().parent.parent / "driptwin_log.db"
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self.kind = "sqlite"
            for q in SCHEMA_SQLITE:
                self.conn.execute(q)

    def _ts(self, wall: float):
        if self.kind == "timescale":
            from datetime import datetime, timezone
            return datetime.fromtimestamp(wall, timezone.utc)
        return wall

    def telemetry(self, s: dict) -> None:
        ph = "%s" if self.kind == "timescale" else "?"
        self.conn.execute(
            f"INSERT INTO telemetry VALUES ({','.join([ph] * 8)})",
            (self._ts(time.time()), self.infusion_id, s["rate_gtt_min"], s["target_gtt_min"],
             s["valve_u"], s["head_cm"], s["infused_ml"], s["mode"]))

    def event(self, kind: str, severity: str, message: str, data: dict) -> None:
        ph = "%s" if self.kind == "timescale" else "?"
        self.conn.execute(
            f"INSERT INTO events VALUES ({','.join([ph] * 6)})",
            (self._ts(time.time()), self.infusion_id, kind, severity, message,
             json.dumps(data, default=str)))
