"""SQLite storage, sync orchestration, unified daily timeline, and CSV/JSON export."""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .client import DAILY_COLLECTIONS, OuraAPIError, OuraClient

DAILY_TIMELINE_FIELDS: list[str] = [
    "day",
    "sleep_score",
    "readiness_score",
    "activity_score",
    "total_sleep_hours",
    "deep_sleep_min",
    "rem_sleep_min",
    "light_sleep_min",
    "sleep_efficiency",
    "average_hrv",
    "lowest_heart_rate",
    "average_breath",
    "temperature_deviation",
    "steps",
    "active_calories",
    "total_calories",
    "stress_summary",
    "stress_high_min",
    "recovery_high_min",
    "resilience_level",
    "spo2_average",
    "workouts_count",
]


def _extract_day(item: dict[str, Any]) -> str:
    if item.get("day"):
        return str(item["day"])[:10]
    for ts_key in ("timestamp", "start_datetime", "bedtime_end", "bedtime_start"):
        val = item.get(ts_key)
        if isinstance(val, str) and len(val) >= 10:
            return val[:10]
    return ""


def _extract_item_id(collection: str, item: dict[str, Any]) -> str:
    if item.get("id"):
        return str(item["id"])
    if collection == "heartrate" and item.get("timestamp"):
        return f"hr:{item['timestamp']}:{item.get('source', '')}"
    if item.get("day"):
        return f"{collection}:{item['day']}"
    digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"{collection}:{digest}"


class OuraStorage:
    """Manages the local SQLite cache of Oura API v2 payloads and aggregated views."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    collection TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    day TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    synced_at REAL NOT NULL,
                    PRIMARY KEY (collection, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_records_collection_day
                    ON records (collection, day);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strava_runs (
                    activity_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT 'Run',
                    sport_type TEXT NOT NULL DEFAULT 'Run',
                    distance_miles REAL NOT NULL DEFAULT 0.0,
                    moving_time_sec INTEGER NOT NULL DEFAULT 0,
                    pace_sec INTEGER NOT NULL DEFAULT 0,
                    pace_str TEXT NOT NULL DEFAULT '-',
                    elevation_gain_ft REAL NOT NULL DEFAULT 0.0,
                    average_hr REAL,
                    max_hr REAL,
                    suffer_score REAL,
                    payload TEXT NOT NULL,
                    synced_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strava_runs_day
                    ON strava_runs (day);
                CREATE TABLE IF NOT EXISTS training_plan_days (
                    day TEXT PRIMARY KEY,
                    week_num INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    workout_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    planned_miles REAL NOT NULL DEFAULT 0.0,
                    target_pace TEXT NOT NULL DEFAULT '-',
                    hill_focus TEXT NOT NULL DEFAULT '',
                    strength_addon TEXT NOT NULL DEFAULT '',
                    is_quality INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    matched_activity_id TEXT,
                    checkin_rpe INTEGER,
                    checkin_soreness INTEGER,
                    checkin_notes TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_training_plan_week
                    ON training_plan_days (week_num);
                CREATE TABLE IF NOT EXISTS plan_adaptations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    target_day TEXT NOT NULL,
                    signal_level TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    previous_days_json TEXT NOT NULL,
                    updated_days_json TEXT NOT NULL,
                    reverted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL DEFAULT 0,
                    compacted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session
                    ON conversation_turns (session_id, compacted, id);
                CREATE TABLE IF NOT EXISTS compacted_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary_text TEXT NOT NULL,
                    compacted_turn_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS long_term_memories (
                    memory_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.85,
                    source_session TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_consolidation_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    extracted_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS hitl_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    action_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'HIGH',
                    proposed_days_json TEXT NOT NULL,
                    previous_days_json TEXT NOT NULL,
                    resolved_at TEXT,
                    resolver_note TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def upsert_records(self, collection: str, items: list[dict[str, Any]]) -> int:
        if not items:
            return 0
        now = time.time()
        rows = [
            (
                collection,
                _extract_item_id(collection, item),
                _extract_day(item),
                json.dumps(item, sort_keys=True),
                now,
            )
            for item in items
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO records (collection, item_id, day, payload, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(collection, item_id) DO UPDATE SET
                    day = excluded.day,
                    payload = excluded.payload,
                    synced_at = excluded.synced_at
                """,
                rows,
            )
        if collection in ("workout", "heartrate"):
            self.sync_oura_workouts_to_runs()
        return len(rows)

    def set_meta(self, key: str, value: Any) -> None:
        encoded = json.dumps(value)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, encoded),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def get_collection(
        self,
        collection: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["collection = ?"]
        params: list[Any] = [collection]
        if start_date:
            clauses.append("day >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("day <= ?")
            params.append(end_date)

        sql = f"SELECT payload FROM records WHERE {' AND '.join(clauses)} ORDER BY day ASC, item_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_collection_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT collection, COUNT(*) AS cnt FROM records GROUP BY collection ORDER BY collection"
            ).fetchall()
        return {str(r["collection"]): int(r["cnt"]) for r in rows}

    def sync_range(
        self,
        client: OuraClient,
        start_date: str,
        end_date: str,
        include_heartrate: bool = True,
    ) -> dict[str, Any]:
        """Pull Oura API v2 collections for [start_date, end_date] and upsert into SQLite."""
        counts: dict[str, int] = {}
        warnings: list[str] = []

        try:
            personal = client.get_personal_info()
            if personal:
                personal_with_id = dict(personal)
                personal_with_id.setdefault("id", "me")
                counts["personal_info"] = self.upsert_records("personal_info", [personal_with_id])
        except OuraAPIError as exc:
            if exc.status in (401, 403, 404):
                warnings.append(f"personal_info skipped (HTTP {exc.status})")
            else:
                raise

        for collection in DAILY_COLLECTIONS:
            try:
                items = client.fetch_collection(collection, start_date=start_date, end_date=end_date)
                counts[collection] = self.upsert_records(collection, items)
            except OuraAPIError as exc:
                if 400 <= exc.status < 500:
                    counts[collection] = 0
                    warnings.append(f"{collection} skipped (HTTP {exc.status}: {exc.detail})")
                else:
                    raise

        if include_heartrate:
            try:
                hr_points = client.fetch_heartrate(start_date=start_date, end_date=end_date)
                counts["heartrate"] = self.upsert_records("heartrate", hr_points)
            except OuraAPIError as exc:
                if 400 <= exc.status < 500:
                    counts["heartrate"] = 0
                    warnings.append(f"heartrate skipped (HTTP {exc.status}: {exc.detail})")
                else:
                    raise

        summary = {
            "start_date": start_date,
            "end_date": end_date,
            "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "counts": counts,
            "warnings": warnings,
        }
        self.set_meta("last_sync", summary)
        return summary

    def get_personal_info(self) -> dict[str, Any] | None:
        items = self.get_collection("personal_info")
        return items[0] if items else None

    def get_daily_timeline(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Merge daily scores, primary sleep session metrics, activity, stress, SpO2, and workouts by day."""
        by_day: dict[str, dict[str, Any]] = {}

        def ensure_day(day: str) -> dict[str, Any]:
            if day not in by_day:
                row: dict[str, Any] = {field: None for field in DAILY_TIMELINE_FIELDS}
                row["day"] = day
                row["workouts_count"] = 0
                row["workouts"] = []
                by_day[day] = row
            return by_day[day]

        for item in self.get_collection("daily_sleep", start_date, end_date):
            day = _extract_day(item)
            if day:
                ensure_day(day)["sleep_score"] = item.get("score")

        # Group detailed sleep sessions by day and pick `long_sleep` or longest sleep duration
        sleep_groups: dict[str, list[dict[str, Any]]] = {}
        for item in self.get_collection("sleep", start_date, end_date):
            day = _extract_day(item)
            if day:
                sleep_groups.setdefault(day, []).append(item)

        for day, periods in sleep_groups.items():
            primary = sorted(
                periods,
                key=lambda s: (
                    1 if s.get("type") == "long_sleep" else 0,
                    int(s.get("total_sleep_duration") or 0),
                ),
                reverse=True,
            )[0]
            entry = ensure_day(day)
            total_sec = primary.get("total_sleep_duration")
            deep_sec = primary.get("deep_sleep_duration")
            rem_sec = primary.get("rem_sleep_duration")
            light_sec = primary.get("light_sleep_duration")

            entry["total_sleep_hours"] = round(total_sec / 3600.0, 2) if total_sec is not None else None
            entry["deep_sleep_min"] = round(deep_sec / 60.0, 1) if deep_sec is not None else None
            entry["rem_sleep_min"] = round(rem_sec / 60.0, 1) if rem_sec is not None else None
            entry["light_sleep_min"] = round(light_sec / 60.0, 1) if light_sec is not None else None
            entry["sleep_efficiency"] = primary.get("efficiency")
            entry["average_hrv"] = primary.get("average_hrv")
            entry["lowest_heart_rate"] = primary.get("lowest_heart_rate")
            entry["average_breath"] = primary.get("average_breath")
            entry["bedtime_start"] = primary.get("bedtime_start")
            entry["bedtime_end"] = primary.get("bedtime_end")

        for item in self.get_collection("daily_readiness", start_date, end_date):
            day = _extract_day(item)
            if day:
                entry = ensure_day(day)
                entry["readiness_score"] = item.get("score")
                entry["temperature_deviation"] = item.get("temperature_deviation")

        for item in self.get_collection("daily_activity", start_date, end_date):
            day = _extract_day(item)
            if day:
                entry = ensure_day(day)
                entry["activity_score"] = item.get("score")
                entry["steps"] = item.get("steps")
                entry["active_calories"] = item.get("active_calories")
                entry["total_calories"] = item.get("total_calories")

        for item in self.get_collection("daily_stress", start_date, end_date):
            day = _extract_day(item)
            if day:
                entry = ensure_day(day)
                entry["stress_summary"] = item.get("day_summary")
                stress_sec = item.get("stress_high")
                rec_sec = item.get("recovery_high")
                entry["stress_high_min"] = round(stress_sec / 60.0, 1) if stress_sec is not None else None
                entry["recovery_high_min"] = round(rec_sec / 60.0, 1) if rec_sec is not None else None

        for item in self.get_collection("daily_resilience", start_date, end_date):
            day = _extract_day(item)
            if day:
                ensure_day(day)["resilience_level"] = item.get("level")

        for item in self.get_collection("daily_spo2", start_date, end_date):
            day = _extract_day(item)
            if day:
                spo2_obj = item.get("spo2_percentage") or {}
                avg_val = spo2_obj.get("average") if isinstance(spo2_obj, dict) else None
                ensure_day(day)["spo2_average"] = round(avg_val, 2) if avg_val is not None else None

        for item in self.get_collection("workout", start_date, end_date):
            day = _extract_day(item)
            if day:
                entry = ensure_day(day)
                entry["workouts_count"] = int(entry["workouts_count"] or 0) + 1
                entry["workouts"].append(
                    {
                        "activity": item.get("activity"),
                        "calories": item.get("calories"),
                        "intensity": item.get("intensity"),
                        "start_datetime": item.get("start_datetime"),
                        "end_datetime": item.get("end_datetime"),
                    }
                )

        return [by_day[d] for d in sorted(by_day.keys())]

    def export_data(
        self,
        output_dir: Path,
        fmt: str = "csv",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Path]:
        """Export unified daily summary and raw collections to CSV or JSON files."""
        output_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        timeline = self.get_daily_timeline(start_date=start_date, end_date=end_date)

        if fmt == "json":
            summary_path = output_dir / "daily_timeline.json"
            summary_path.write_text(json.dumps(timeline, indent=2) + "\n", encoding="utf-8")
            written.append(summary_path)

            for col in (*DAILY_COLLECTIONS, "heartrate", "personal_info"):
                items = self.get_collection(col, start_date=start_date, end_date=end_date)
                if items:
                    col_path = output_dir / f"{col}.json"
                    col_path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
                    written.append(col_path)
        else:
            summary_path = output_dir / "daily_timeline.csv"
            with summary_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=DAILY_TIMELINE_FIELDS,
                    extrasaction="ignore",
                )
                writer.writeheader()
                for row in timeline:
                    writer.writerow(row)
            written.append(summary_path)

        return written

    def format_timeline_csv(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Render the unified daily timeline as an in-memory CSV string for browser download."""
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=DAILY_TIMELINE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in self.get_daily_timeline(start_date=start_date, end_date=end_date):
            writer.writerow(row)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Strava GPS Runs & Half-Marathon Training Plan Persistence
    # ------------------------------------------------------------------

    def upsert_strava_runs(self, runs: list[dict[str, Any]]) -> int:
        if not runs:
            return 0
        now = time.time()
        rows = [
            (
                str(r["activity_id"]),
                str(r["day"]),
                str(r.get("name") or "Run"),
                str(r.get("sport_type") or "Run"),
                float(r.get("distance_miles") or 0.0),
                int(r.get("moving_time_sec") or 0),
                int(r.get("pace_sec") or 0),
                str(r.get("pace_str") or "-"),
                float(r.get("elevation_gain_ft") or 0.0),
                r.get("average_hr"),
                r.get("max_hr"),
                r.get("suffer_score"),
                json.dumps(r.get("raw") or r, sort_keys=True),
                now,
            )
            for r in runs
            if r.get("activity_id") and r.get("day")
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO strava_runs (
                    activity_id, day, name, sport_type, distance_miles, moving_time_sec,
                    pace_sec, pace_str, elevation_gain_ft, average_hr, max_hr, suffer_score, payload, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    day = excluded.day,
                    name = excluded.name,
                    sport_type = excluded.sport_type,
                    distance_miles = excluded.distance_miles,
                    moving_time_sec = excluded.moving_time_sec,
                    pace_sec = excluded.pace_sec,
                    pace_str = excluded.pace_str,
                    elevation_gain_ft = excluded.elevation_gain_ft,
                    average_hr = excluded.average_hr,
                    max_hr = excluded.max_hr,
                    suffer_score = excluded.suffer_score,
                    payload = excluded.payload,
                    synced_at = excluded.synced_at
                """,
                rows,
            )
        self.match_strava_runs_to_plan()
        return len(rows)

    def get_strava_runs(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_date:
            clauses.append("day >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("day <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT activity_id, day, name, sport_type, distance_miles, moving_time_sec, "
            "pace_sec, pace_str, elevation_gain_ft, average_hr, max_hr, suffer_score "
            f"FROM strava_runs {where} ORDER BY day ASC, distance_miles DESC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_plan_days(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM training_plan_days").fetchone()
        return int(row["cnt"]) if row else 0

    def upsert_plan_days(self, days: list[dict[str, Any]], overwrite_existing: bool = True) -> int:
        if not days:
            return 0
        now = time.time()
        rows = [
            (
                str(d["day"]),
                int(d["week_num"]),
                str(d["phase"]),
                str(d["workout_type"]),
                str(d["title"]),
                str(d.get("description") or ""),
                float(d.get("planned_miles") or 0.0),
                str(d.get("target_pace") or "-"),
                str(d.get("hill_focus") or ""),
                str(d.get("strength_addon") or ""),
                1 if d.get("is_quality") else 0,
                str(d.get("status") or "scheduled"),
                d.get("matched_activity_id"),
                d.get("checkin_rpe"),
                d.get("checkin_soreness"),
                str(d.get("checkin_notes") or ""),
                now,
            )
            for d in days
        ]
        conflict_clause = (
            """
            DO UPDATE SET
                week_num = excluded.week_num,
                phase = excluded.phase,
                workout_type = excluded.workout_type,
                title = excluded.title,
                description = excluded.description,
                planned_miles = excluded.planned_miles,
                target_pace = excluded.target_pace,
                hill_focus = excluded.hill_focus,
                strength_addon = excluded.strength_addon,
                is_quality = excluded.is_quality,
                status = excluded.status,
                matched_activity_id = excluded.matched_activity_id,
                checkin_rpe = excluded.checkin_rpe,
                checkin_soreness = excluded.checkin_soreness,
                checkin_notes = excluded.checkin_notes,
                updated_at = excluded.updated_at
            """
            if overwrite_existing
            else "DO NOTHING"
        )
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO training_plan_days (
                    day, week_num, phase, workout_type, title, description,
                    planned_miles, target_pace, hill_focus, strength_addon,
                    is_quality, status, matched_activity_id, checkin_rpe,
                    checkin_soreness, checkin_notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) {conflict_clause}
                """,
                rows,
            )
        self.match_strava_runs_to_plan()
        return len(rows)

    def sync_oura_workouts_to_runs(self) -> int:
        """Normalize Oura `/v2/usercollection/workout` running sessions (+ Oura HR samples) into training runs."""
        workouts = self.get_collection("workout")
        if not workouts:
            return 0

        # Group Oura heartrate points by day for fast workout HR enrichment
        hr_by_day: dict[str, list[tuple[dt.datetime, float]]] = {}
        for hr_item in self.get_collection("heartrate"):
            ts_raw = hr_item.get("timestamp")
            bpm = hr_item.get("bpm")
            if isinstance(ts_raw, str) and bpm is not None:
                try:
                    ts_dt = dt.datetime.fromisoformat(ts_raw)
                    hr_by_day.setdefault(ts_raw[:10], []).append((ts_dt, float(bpm)))
                except ValueError:
                    pass

        normalized_runs: list[dict[str, Any]] = []
        for item in workouts:
            act = str(item.get("activity") or "").lower()
            label = str(item.get("label") or "").lower()
            if "run" not in act and "jog" not in act and "run" not in label and "jog" not in label:
                continue

            day = _extract_day(item)
            if not day:
                continue

            start_str = item.get("start_datetime")
            end_str = item.get("end_datetime")
            moving_sec = 0
            start_dt: dt.datetime | None = None
            end_dt: dt.datetime | None = None
            if isinstance(start_str, str) and isinstance(end_str, str):
                try:
                    start_dt = dt.datetime.fromisoformat(start_str)
                    end_dt = dt.datetime.fromisoformat(end_str)
                    moving_sec = max(0, int((end_dt - start_dt).total_seconds()))
                except ValueError:
                    pass

            dist_m = item.get("distance")
            intensity = str(item.get("intensity") or "moderate").lower()
            if dist_m is not None and float(dist_m) > 50:
                miles = round(float(dist_m) / 1609.344, 2)
                pace_sec = int(round(moving_sec / miles)) if miles > 0 and moving_sec > 0 else 0
                mins, secs = divmod(pace_sec, 60)
                pace_str = f"{mins}:{secs:02d}/mi" if pace_sec > 0 else "Oura GPS"
            else:
                # Auto-detected Oura ring run without phone GPS: estimate miles from duration & intensity
                est_pace = 630 if intensity == "easy" else (535 if intensity == "hard" else 585)
                miles = round(moving_sec / float(est_pace), 2) if moving_sec >= 300 else 0.0
                pace_sec = est_pace if miles > 0 else 0
                dur_m = max(1, round(moving_sec / 60)) if moving_sec > 0 else 0
                pace_str = f"~{dur_m}m ({intensity} Oura)" if dur_m else f"Oura ({intensity})"

            avg_hr: float | None = None
            max_hr: float | None = None
            if start_dt is not None and end_dt is not None:
                candidates = hr_by_day.get(day, [])
                matched_bpms = [
                    bpm for ts, bpm in candidates
                    if ts.tzinfo is not None and start_dt.tzinfo is not None and start_dt <= ts <= end_dt
                ]
                if matched_bpms:
                    avg_hr = round(sum(matched_bpms) / len(matched_bpms), 1)
                    max_hr = round(max(matched_bpms), 1)

            item_id = _extract_item_id("workout", item)
            title = str(item.get("label") or item.get("activity") or "Oura Run").replace("_", " ").title()
            normalized_runs.append(
                {
                    "activity_id": f"oura:{item_id}",
                    "day": day,
                    "name": title,
                    "sport_type": "OuraRun",
                    "distance_miles": miles,
                    "moving_time_sec": moving_sec,
                    "pace_sec": pace_sec,
                    "pace_str": pace_str,
                    "elevation_gain_ft": 0.0,
                    "average_hr": avg_hr,
                    "max_hr": max_hr,
                    "suffer_score": float(item["calories"]) if item.get("calories") is not None else None,
                    "raw": item,
                }
            )

        return self.upsert_strava_runs(normalized_runs)

    def match_strava_runs_to_plan(self) -> None:
        """Link daily completed runs (manual override first, then Oura/GPS runs) to training_plan_days."""
        with self._connect() as conn:
            runs = conn.execute(
                """
                SELECT activity_id, day, distance_miles
                FROM strava_runs
                ORDER BY
                    day ASC,
                    CASE WHEN activity_id LIKE 'manual:%' THEN 0 ELSE 1 END ASC,
                    distance_miles DESC
                """
            ).fetchall()
            primary_by_day: dict[str, str] = {}
            for r in runs:
                d = str(r["day"])
                if d not in primary_by_day:
                    primary_by_day[d] = str(r["activity_id"])
            for day, act_id in primary_by_day.items():
                conn.execute(
                    """
                    UPDATE training_plan_days
                    SET matched_activity_id = ?, status = 'completed'
                    WHERE day = ?
                    """,
                    (act_id, day),
                )

    def get_plan_days(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        week_num: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if week_num is not None:
            clauses.append("p.week_num = ?")
            params.append(int(week_num))
        if start_date:
            clauses.append("p.day >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("p.day <= ?")
            params.append(end_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT
                p.*,
                s.name AS actual_run_name,
                s.distance_miles AS actual_miles,
                s.pace_str AS actual_pace,
                s.elevation_gain_ft AS actual_elevation_ft,
                s.average_hr AS actual_avg_hr
            FROM training_plan_days p
            LEFT JOIN strava_runs s ON p.matched_activity_id = s.activity_id
            {where}
            ORDER BY p.day ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["is_quality"] = bool(item.get("is_quality"))
            out.append(item)
        return out

    def get_plan_day(self, day: str) -> dict[str, Any] | None:
        days = self.get_plan_days(start_date=day, end_date=day)
        return days[0] if days else None

    def save_checkin(
        self,
        day: str,
        rpe: int | None = None,
        soreness: int | None = None,
        notes: str | None = None,
        miles: float | None = None,
        duration_min: float | None = None,
        elevation_ft: float | None = None,
    ) -> dict[str, Any] | None:
        existing = self.get_plan_day(day)
        if not existing:
            return None
        new_rpe = rpe if rpe is not None else existing.get("checkin_rpe")
        new_soreness = soreness if soreness is not None else existing.get("checkin_soreness")
        new_notes = notes if notes is not None else existing.get("checkin_notes", "")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE training_plan_days
                SET checkin_rpe = ?, checkin_soreness = ?, checkin_notes = ?, updated_at = ?
                WHERE day = ?
                """,
                (new_rpe, new_soreness, str(new_notes), time.time(), day),
            )

        if miles is not None and float(miles) > 0:
            mi_val = round(float(miles), 2)
            dur_sec = int(round(float(duration_min) * 60)) if duration_min and float(duration_min) > 0 else 0
            pace_sec = int(round(dur_sec / mi_val)) if dur_sec > 0 and mi_val > 0 else 0
            mins, secs = divmod(pace_sec, 60)
            pace_str = f"{mins}:{secs:02d}/mi" if pace_sec > 0 else "Logged Run"
            self.upsert_strava_runs(
                [
                    {
                        "activity_id": f"manual:{day}",
                        "day": day,
                        "name": existing.get("title") or "Logged Run",
                        "sport_type": "Run",
                        "distance_miles": mi_val,
                        "moving_time_sec": dur_sec,
                        "pace_sec": pace_sec,
                        "pace_str": pace_str,
                        "elevation_gain_ft": float(elevation_ft or 0.0),
                        "average_hr": None,
                        "max_hr": None,
                        "suffer_score": None,
                        "raw": {"source": "manual_checkin", "day": day, "miles": mi_val, "duration_min": duration_min},
                    }
                ]
            )

        return self.get_plan_day(day)

    def record_adaptation(
        self,
        target_day: str,
        signal_level: str,
        reason: str,
        metrics: dict[str, Any],
        previous_days: list[dict[str, Any]],
        updated_days: list[dict[str, Any]],
    ) -> int:
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO plan_adaptations (
                    created_at, target_day, signal_level, reason,
                    metrics_json, previous_days_json, updated_days_json, reverted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    created_at,
                    target_day,
                    signal_level,
                    reason,
                    json.dumps(metrics, sort_keys=True),
                    json.dumps(previous_days, sort_keys=True),
                    json.dumps(updated_days, sort_keys=True),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_adaptations(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plan_adaptations ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            d["previous_days"] = json.loads(d.pop("previous_days_json") or "[]")
            d["updated_days"] = json.loads(d.pop("updated_days_json") or "[]")
            results.append(d)
        return results

    def undo_adaptation(self, adaptation_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan_adaptations WHERE id = ?",
                (int(adaptation_id),),
            ).fetchone()
        if not row:
            raise ValueError(f"Adaptation #{adaptation_id} not found.")
        if row["reverted_at"]:
            raise ValueError(f"Adaptation #{adaptation_id} was already undone.")

        prev_days: list[dict[str, Any]] = json.loads(row["previous_days_json"])
        self.upsert_plan_days(prev_days, overwrite_existing=True)

        reverted_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE plan_adaptations SET reverted_at = ? WHERE id = ?",
                (reverted_ts, int(adaptation_id)),
            )
        return {
            "undone_id": int(adaptation_id),
            "restored_dates": [d["day"] for d in prev_days],
            "reverted_at": reverted_ts,
        }
