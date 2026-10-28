#!/usr/bin/env python3
"""Append-only race prediction store, training, evaluation and walk-forward tests."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from domain import (  # noqa: E402
    MIN_QUOTE_CHARS,
    derive_place_slots,
    normalise_text,
    quote_supported,
    registered as feature_registered,
    describe as describe_features,
)
from drift import period_of, summarize_periods  # noqa: E402
from decision import coverage_slices, settle_stream  # noqa: E402
from racing_model import (  # noqa: E402
    apply_calibration,
    bootstrap_logloss_difference,
    expected_calibration_error,
    information_gain_vs_market,
    isotonic_fit,
    platt_fit,
    fit_model,
    metric_row,
    predict_card,
    summarize_rows,
)

VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FAMILYWISE_ALPHA = 0.05
CALIBRATION_ISOTONIC_MIN_SAMPLES = 500
IMMUTABLE_TABLES = [
    "card_snapshots",
    "result_snapshots",
    "prediction_runs",
    "models",
    "model_events",
    "evaluation_runs",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def read_json(path: str) -> dict:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_or_print(payload: dict, output: str | None, summary: dict | None = None) -> None:
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        notice = summary or {"output": str(destination.resolve())}
        notice.setdefault("output", str(destination.resolve()))
        print(json.dumps(notice, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS races (
            race_id TEXT PRIMARY KEY,
            meeting_date TEXT NOT NULL,
            off_time TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS card_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            race_id TEXT NOT NULL REFERENCES races(race_id),
            captured_at TEXT NOT NULL,
            data_cutoff TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cards_race ON card_snapshots(race_id, captured_at);
        CREATE TABLE IF NOT EXISTS result_snapshots (
            result_id TEXT PRIMARY KEY,
            race_id TEXT NOT NULL REFERENCES races(race_id),
            recorded_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_results_race ON result_snapshots(race_id, recorded_at);
        CREATE TABLE IF NOT EXISTS models (
            model_version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algorithm TEXT NOT NULL,
            config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_events (
            event_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL REFERENCES models(model_version),
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_events ON model_events(model_version, event_type, created_at);
        CREATE TABLE IF NOT EXISTS prediction_runs (
            run_id TEXT PRIMARY KEY,
            race_id TEXT NOT NULL REFERENCES races(race_id),
            snapshot_id TEXT NOT NULL REFERENCES card_snapshots(snapshot_id),
            model_version TEXT NOT NULL,
            algorithm TEXT NOT NULL,
            forecast_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data_cutoff TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_predictions ON prediction_runs(model_version, race_id, created_at);
        CREATE TABLE IF NOT EXISTS evaluation_runs (
            evaluation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            model_version TEXT,
            created_at TEXT NOT NULL,
            report_json TEXT NOT NULL
        );
        """)
        for table in IMMUTABLE_TABLES:
            db.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
            """)


def validate_card(card: dict, *, allow_unknown_features: bool = False) -> list[str]:
    schema = card.get("schema_version")
    if schema not in {"1.0", "1.1"}:
        raise ValueError("Card schema_version must be '1.0' or '1.1'")
    strict_evidence = schema == "1.1"
    race = card.get("race", {})
    for field in ["race_id", "meeting_date", "venue", "race_no", "off_time", "distance_m"]:
        if field not in race:
            raise ValueError(f"Missing race field: {field}")
    if not isinstance(race["race_id"], str) or not race["race_id"].strip():
        raise ValueError("race.race_id must be a non-empty string")
    datetime.strptime(race["meeting_date"], "%Y-%m-%d")
    off_time = parse_ts(race["off_time"], "race.off_time")
    cutoff = parse_ts(card.get("data_cutoff", ""), "data_cutoff")
    captured = parse_ts(card.get("captured_at", ""), "captured_at")
    if cutoff > off_time:
        raise ValueError("data_cutoff is after off_time; possible look-ahead leakage")
    if cutoff > captured:
        raise ValueError("data_cutoff cannot be later than captured_at")
    mode = card.get("collection_mode", "historical")
    if mode not in {"live", "historical"}:
        raise ValueError("collection_mode must be live or historical")
    if mode == "live" and captured > off_time:
        raise ValueError("A live card cannot be captured after off_time")
    late_sources = card.get("retrieval_after_cutoff", False)
    if not isinstance(late_sources, bool):
        raise ValueError("retrieval_after_cutoff must be a boolean")
    if late_sources and mode != "historical":
        raise ValueError("retrieval_after_cutoff is only allowed for collection_mode: historical")
    if not isinstance(race.get("race_no"), int) or race["race_no"] < 1:
        raise ValueError("race.race_no must be a positive integer")
    if not isinstance(race.get("distance_m"), int) or race["distance_m"] <= 0:
        raise ValueError("race.distance_m must be a positive integer")
    declared = race.get("declared_starters")
    if declared is not None and (isinstance(declared, bool) or not isinstance(declared, int) or declared < 2):
        raise ValueError("race.declared_starters must be an integer of at least 2")
    slots = race.get("place_slots")
    if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= 3):
        raise ValueError("race.place_slots must be 1, 2 or 3")

    sources = card.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("At least one source is required")
    source_ids = set()
    for source in sources:
        source_id = source.get("source_id")
        if not source_id or source_id in source_ids:
            raise ValueError("Source IDs must be non-empty and unique")
        source_ids.add(source_id)
        if not source.get("title"):
            raise ValueError(f"Source {source_id} needs a title")
        if strict_evidence and not source.get("url"):
            raise ValueError(f"Source {source_id} needs a url in schema 1.1")
        if source.get("excerpt") is not None and not isinstance(source["excerpt"], str):
            raise ValueError(f"Source {source_id} excerpt must be a string")
        if source.get("retrieved_at"):
            retrieved = parse_ts(source["retrieved_at"], f"source {source_id} retrieved_at")
            if strict_evidence and retrieved > cutoff and not late_sources:
                raise ValueError(
                    f"Source {source_id} was retrieved after data_cutoff; for a post-hoc "
                    "reconstruction set retrieval_after_cutoff: true with collection_mode: historical"
                )

    runners = card.get("runners")
    if not isinstance(runners, list) or len(runners) < 2:
        raise ValueError("At least two runners are required")
    numbers = set()
    active = 0
    banned_features = {"finish_position", "final_position", "race_result", "dividend", "payout"}
    for runner in runners:
        number = runner.get("horse_no")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("horse_no must be a positive integer")
        if number in numbers:
            raise ValueError(f"Duplicate horse_no: {number}")
        numbers.add(number)
        if not runner.get("horse_name"):
            raise ValueError(f"Horse {number} needs horse_name")
        if not runner.get("scratched", False):
            active += 1
        odds = runner.get("win_odds")
        if odds is not None and (
            isinstance(odds, bool) or not isinstance(odds, (int, float))
            or not math.isfinite(float(odds)) or float(odds) <= 1.0
        ):
            raise ValueError(f"Horse {number} has invalid win_odds")
        features = runner.get("features", {})
        if not isinstance(features, dict):
            raise ValueError(f"Horse {number} features must be an object")
        for name, value in features.items():
            if name in banned_features:
                raise ValueError(f"Post-race feature is forbidden: {name}")
            if not allow_unknown_features and not feature_registered(name):
                raise ValueError(
                    f"Unregistered feature on horse {number}: {name}. "
                    "Register it in scripts/domain.py, or pass --allow-unknown-features "
                    "to accept the leakage risk."
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"Feature {name} on horse {number} must be numeric")
        evidence = runner.get("intent_evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"Horse {number} intent_evidence must be a list")
        for item in evidence:
            if not item.get("signal") or not item.get("note"):
                raise ValueError(f"Horse {number} evidence needs signal and note")
            if item.get("source_id") not in source_ids:
                raise ValueError(f"Horse {number} evidence has an unknown source_id")
            if strict_evidence:
                quote = item.get("quote")
                if not isinstance(quote, str) or len(normalise_text(quote)) < MIN_QUOTE_CHARS:
                    raise ValueError(
                        f"Horse {number} evidence needs a verbatim quote of at least "
                        f"{MIN_QUOTE_CHARS} characters in schema 1.1"
                    )
                excerpt = next(
                    (source.get("excerpt", "") for source in sources if source["source_id"] == item["source_id"]),
                    "",
                )
                if excerpt and not quote_supported(quote, excerpt):
                    raise ValueError(
                        f"Horse {number} quote was not found in the excerpt of source {item['source_id']}"
                    )
            direction = item.get("direction", "neutral")
            if not isinstance(direction, (int, float)) and str(direction).lower() not in {
                "positive", "negative", "neutral", "1", "-1", "0"
            }:
                raise ValueError("Evidence direction must be positive, negative, neutral, -1, 0 or 1")
            for field in ["strength", "confidence"]:
                value = item.get(field, 0.5)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
                    raise ValueError(f"Evidence {field} must be between 0 and 1")
    if active < 2:
        raise ValueError("At least two active runners are required")
    reference_field = declared if declared is not None else active
    expected_slots = derive_place_slots(reference_field)
    warnings = []
    if slots is None:
        race["place_slots"] = expected_slots
    elif slots != expected_slots:
        warnings.append(
            f"place_slots={slots} but {reference_field} declared starters imply {expected_slots}; "
            "the card value is used for place probabilities."
        )
    if mode == "historical":
        warnings.append("歷史重建資料：只可當回測輸入，唔可以冒充當日實時賽前預測。")
    if not strict_evidence:
        warnings.append("schema 1.0 卡：意圖證據未有逐字 quote，建議改用 schema 1.1。")
    if late_sources:
        warnings.append(
            "事後重建：部分來源喺 data_cutoff 之後取得（retrieval_after_cutoff=true），"
            "只可作回測輸入，唔可以當賽前快照。"
        )
    return warnings


def validate_result(result: dict, card: dict) -> None:
    if result.get("schema_version") != "1.0":
        raise ValueError("Result schema_version must be '1.0'")
    if result.get("race_id") != card["race"]["race_id"]:
        raise ValueError("Result race_id does not match the card")
    recorded = parse_ts(result.get("recorded_at", ""), "recorded_at")
    if recorded < parse_ts(card["race"]["off_time"], "race.off_time"):
        raise ValueError("Result cannot be recorded before off_time")
    rows = result.get("results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Result rows are required")
    allowed = {int(r["horse_no"]) for r in card["runners"]}
    seen = set()
    winners = 0
    for row in rows:
        number = row.get("horse_no")
        if isinstance(number, bool) or not isinstance(number, int) or number not in allowed or number in seen:
            raise ValueError(f"Invalid or duplicate result horse_no: {number}")
        seen.add(number)
        position = row.get("finish_position")
        if position is not None and (isinstance(position, bool) or not isinstance(position, int) or position < 1):
            raise ValueError("finish_position must be null or a positive integer")
        if position == 1:
            winners += 1
    if winners < 1:
        raise ValueError("A completed result needs at least one winner")


def latest_card(db: sqlite3.Connection, race_id: str, snapshot_id: str | None = None) -> tuple[dict, str]:
    if snapshot_id:
        row = db.execute(
            "SELECT * FROM card_snapshots WHERE race_id = ? AND snapshot_id = ?", (race_id, snapshot_id)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM card_snapshots WHERE race_id = ? ORDER BY captured_at DESC, rowid DESC LIMIT 1", (race_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"No card snapshot for race: {race_id}")
    return json.loads(row["payload_json"]), row["snapshot_id"]


def latest_result(db: sqlite3.Connection, race_id: str) -> dict | None:
    row = db.execute(
        "SELECT payload_json FROM result_snapshots WHERE race_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
        (race_id,),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def load_records(db: sqlite3.Connection, *, train_before: str | None = None) -> list[dict]:
    query = "SELECT race_id FROM races"
    params: list[str] = []
    if train_before:
        query += " WHERE meeting_date < ?"
        params.append(train_before)
    query += " ORDER BY off_time, race_id"
    records = []
    for row in db.execute(query, params).fetchall():
        card, _ = latest_card(db, row["race_id"])
        result = latest_result(db, row["race_id"])
        if result:
            records.append({"card": card, "result": result})
    records.sort(key=lambda item: (parse_ts(item["card"]["race"]["off_time"], "off_time"), item["card"]["race"]["race_id"]))
    return records


def get_model(db: sqlite3.Connection, version: str) -> dict:
    row = db.execute("SELECT config_json FROM models WHERE model_version = ?", (version,)).fetchone()
    if not row:
        raise ValueError(f"Unknown model version: {version}")
    return json.loads(row["config_json"])


def latest_promoted(db: sqlite3.Connection) -> str | None:
    row = db.execute(
        "SELECT model_version FROM model_events WHERE event_type = 'promoted' ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return row["model_version"] if row else None


def parse_blocks(entries: list[str] | None) -> dict[str, list[str]]:
    """Parse repeatable ``name=feat1,feat2`` arguments into feature blocks."""
    blocks: dict[str, list[str]] = {}
    for entry in entries or []:
        name, _, raw = entry.partition("=")
        name = name.strip()
        features = [item.strip() for item in raw.split(",") if item.strip()]
        if not name or not features:
            raise ValueError(f"Feature block must look like name=feature,feature: {entry}")
        if name in blocks:
            raise ValueError(f"Duplicate feature block: {name}")
        blocks[name] = features
    return blocks


def stripped_row(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "calibration"}


def command_import_card(args) -> dict:
    card = read_json(args.file)
    warnings = validate_card(card, allow_unknown_features=args.allow_unknown_features)
    race = card["race"]
    digest = payload_hash(card)
    with connect(args.db) as db:
        db.execute(
            "INSERT OR IGNORE INTO races(race_id, meeting_date, off_time, created_at) VALUES (?, ?, ?, ?)",
            (race["race_id"], race["meeting_date"], race["off_time"], now_iso()),
        )
        duplicate = db.execute(
            "SELECT snapshot_id FROM card_snapshots WHERE race_id = ? AND payload_hash = ? ORDER BY rowid DESC LIMIT 1",
            (race["race_id"], digest),
        ).fetchone()
        if duplicate:
            return {"race_id": race["race_id"], "snapshot_id": duplicate["snapshot_id"], "duplicate": True, "warnings": warnings}
        snapshot_id = "cs_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO card_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, race["race_id"], card["captured_at"], card["data_cutoff"], digest, canonical(card)),
        )
    return {"race_id": race["race_id"], "snapshot_id": snapshot_id, "duplicate": False, "warnings": warnings}


def command_import_odds(args) -> dict:
    """Append a NEW card snapshot with updated odds (never mutates history)."""
    payload = read_json(args.file)
    file_race_id = payload.get("race_id")
    if args.race_id and file_race_id and args.race_id != file_race_id:
        raise ValueError("race_id mismatch between --race-id and the odds file")
    race_id = args.race_id or file_race_id
    if not race_id:
        raise ValueError("race_id is required (flag or file)")

    with connect(args.db) as db:
        if db.execute("SELECT 1 FROM result_snapshots WHERE race_id = ? LIMIT 1", (race_id,)).fetchone():
            raise ValueError("Result already imported for this race; refusing to back-fill odds")
        card, base_snapshot = latest_card(db, race_id)
        validate_card(card)
        off_time = parse_ts(card["race"]["off_time"], "race.off_time")
        captured = parse_ts(payload.get("captured_at", ""), "captured_at")
        if captured > off_time:
            raise ValueError("odds captured_at is after off_time; refusing look-ahead")
        entries = payload.get("odds")
        if not isinstance(entries, list) or not entries:
            raise ValueError("odds must be a non-empty list")

        runner_map = {int(r["horse_no"]): r for r in card["runners"]}
        seen = set()
        for entry in entries:
            number = entry.get("horse_no")
            if not isinstance(number, int) or isinstance(number, bool):
                raise ValueError(f"horse_no must be an integer: {entry!r}")
            if number in seen:
                raise ValueError(f"duplicate horse_no in odds file: {number}")
            seen.add(number)
            if number not in runner_map:
                raise ValueError(f"unknown horse_no: {number}")
            if runner_map[number].get("scratched"):
                raise ValueError(f"horse {number} is scratched")
            win_odds = entry.get("win_odds")
            if (
                not isinstance(win_odds, (int, float))
                or isinstance(win_odds, bool)
                or not math.isfinite(float(win_odds))
                or float(win_odds) <= 1.0
            ):
                raise ValueError(f"win_odds for horse {number} must be a finite number > 1.0")
            place_odds = entry.get("place_odds")
            if place_odds is not None and (
                not isinstance(place_odds, (int, float))
                or isinstance(place_odds, bool)
                or not math.isfinite(float(place_odds))
                or float(place_odds) <= 1.0
            ):
                raise ValueError(f"place_odds for horse {number} must be > 1.0 when provided")

        updated = copy.deepcopy(card)
        applied = []
        for runner in updated["runners"]:
            entry = next((e for e in entries if e.get("horse_no") == runner["horse_no"]), None)
            if entry is None:
                continue
            runner["win_odds"] = float(entry["win_odds"])
            if entry.get("place_odds") is not None:
                runner["place_odds"] = float(entry["place_odds"])
            applied.append(runner["horse_no"])

        updated["captured_at"] = captured.isoformat()
        updated["data_cutoff"] = captured.isoformat()
        source = payload.get("source") or {}
        source_id = str(source.get("source_id") or "ODDS")
        if not any(item.get("source_id") == source_id for item in updated["sources"]):
            updated["sources"].append({
                "source_id": source_id,
                "title": source.get("title") or "Odds snapshot",
                "url": source.get("url"),
                "retrieved_at": captured.isoformat(),
                "official": bool(source.get("official", False)),
            })
        updated["odds_snapshot"] = {
            "captured_at": captured.isoformat(),
            "source_id": source_id,
            "base_snapshot_id": base_snapshot,
            "minutes_to_off": round((off_time - captured).total_seconds() / 60.0, 2),
            "quoted_runners": len(applied),
        }
        validate_card(updated)

        digest = payload_hash(updated)
        duplicate = db.execute(
            "SELECT snapshot_id FROM card_snapshots WHERE race_id = ? AND payload_hash = ? ORDER BY rowid DESC LIMIT 1",
            (race_id, digest),
        ).fetchone()
        if duplicate:
            return {
                "race_id": race_id,
                "snapshot_id": duplicate["snapshot_id"],
                "duplicate": True,
                "updated_runners": applied,
            }
        snapshot_id = "cs_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO card_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, race_id, updated["captured_at"], updated["data_cutoff"], digest, canonical(updated)),
        )

        active = [r for r in updated["runners"] if not r.get("scratched")]
        priced = [r for r in active if isinstance(r.get("win_odds"), (int, float)) and float(r["win_odds"]) > 1.0]
    return {
        "race_id": race_id,
        "snapshot_id": snapshot_id,
        "base_snapshot_id": base_snapshot,
        "duplicate": False,
        "updated_runners": applied,
        "priced_runners": len(priced),
        "active_runners": len(active),
        "market_completeness": round(len(priced) / len(active), 4) if active else 0.0,
        "minutes_to_off": updated["odds_snapshot"]["minutes_to_off"],
    }


def command_diagnose_card(args) -> dict:
    """Report card completeness so gaps are visible BEFORE prediction."""
    with connect(args.db) as db:
        if getattr(args, "race_id", None):
            race_ids = [args.race_id]
        else:
            race_ids = [row["race_id"] for row in db.execute("SELECT race_id FROM races ORDER BY meeting_date, race_id")]
        reports = []
        for race_id in race_ids:
            card, snapshot_id = latest_card(db, race_id)
            active = [r for r in card["runners"] if not r.get("scratched")]
            priced = [r for r in active if isinstance(r.get("win_odds"), (int, float)) and float(r["win_odds"]) > 1.0]
            priced_numbers = {r["horse_no"] for r in priced}
            optional = {
                field: sum(1 for r in active if r.get(field) in (None, ""))
                for field in ("jockey", "trainer", "draw", "weight_lb", "rating")
            }
            reports.append({
                "race_id": race_id,
                "snapshot_id": snapshot_id,
                "runners": len(active),
                "priced_runners": len(priced),
                "market_completeness": round(len(priced) / len(active), 4) if active else 0.0,
                "missing_odds_horse_no": [r["horse_no"] for r in active if r["horse_no"] not in priced_numbers],
                "missing_optional_field_counts": optional,
                "odds_snapshot": card.get("odds_snapshot"),
            })
    return {"races": reports}


def command_import_result(args) -> dict:
    result = read_json(args.file)
    with connect(args.db) as db:
        card, _ = latest_card(db, result.get("race_id", ""))
        validate_result(result, card)
        digest = payload_hash(result)
        duplicate = db.execute(
            "SELECT result_id FROM result_snapshots WHERE race_id = ? AND payload_hash = ? ORDER BY rowid DESC LIMIT 1",
            (result["race_id"], digest),
        ).fetchone()
        if duplicate:
            return {"race_id": result["race_id"], "result_id": duplicate["result_id"], "duplicate": True}
        result_id = "rs_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO result_snapshots VALUES (?, ?, ?, ?, ?)",
            (result_id, result["race_id"], result["recorded_at"], digest, canonical(result)),
        )
    return {"race_id": result["race_id"], "result_id": result_id, "duplicate": False}


def command_predict(args) -> dict:
    with connect(args.db) as db:
        card, snapshot_id = latest_card(db, args.race_id, args.snapshot_id)
        if latest_result(db, args.race_id):
            raise ValueError("Results already exist; refusing to create a retrospective prediction")
        off_time = parse_ts(card["race"]["off_time"], "race.off_time")
        historical = args.historical or card.get("collection_mode", "historical") == "historical"
        if datetime.now(timezone.utc) > off_time and not historical:
            raise ValueError("Race has started; use backtest instead of a retrospective prediction")
        version = args.model_version
        model = None
        if version == "auto":
            promoted = latest_promoted(db)
            if promoted:
                version = promoted
                model = get_model(db, promoted)
            else:
                version = "market-baseline"
        elif version not in {"baseline", "market-baseline"}:
            model = get_model(db, version)
        prediction = predict_card(card, model, version)
        run_id = "pr_" + uuid.uuid4().hex
        forecast_kind = "historical-reconstruction" if historical else "pre-race"
        prediction.update({
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "created_at": now_iso(),
            "forecast_kind": forecast_kind,
        })
        db.execute(
            "INSERT INTO prediction_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, args.race_id, snapshot_id, prediction["model_version"], prediction["algorithm"],
                forecast_kind, prediction["created_at"], card["data_cutoff"], canonical(prediction),
            ),
        )
    return prediction


def command_evaluate(args) -> dict:
    with connect(args.db) as db:
        query = "SELECT * FROM prediction_runs"
        params: list[str] = []
        if args.model_version:
            query += " WHERE model_version = ?"
            params.append(args.model_version)
        query += " ORDER BY created_at, rowid"
        kinds = None if args.all_kinds else {
            item.strip() for item in args.kinds.split(",") if item.strip()
        }
        selected = {}
        excluded: dict[str, int] = {}
        for row in db.execute(query, params).fetchall():
            kind = row["forecast_kind"]
            if kinds is not None and kind not in kinds:
                excluded[kind] = excluded.get(kind, 0) + 1
                continue
            selected[(row["model_version"], row["race_id"])] = row
        groups = {}
        pending = 0
        for (version, race_id), row in selected.items():
            card, _ = latest_card(db, race_id, row["snapshot_id"])
            result = latest_result(db, race_id)
            if not result:
                pending += 1
                continue
            prediction = json.loads(row["payload_json"])
            metrics = metric_row(card, result, prediction)
            if not metrics:
                continue
            top_probability = max(
                (float(item["win_probability"]) for item in prediction["runners"]), default=0.0
            )
            groups.setdefault(
                version, {"rows": [], "run_ids": [], "race_ids": [], "confidence": []}
            )
            groups[version]["rows"].append(metrics)
            groups[version]["run_ids"].append(row["run_id"])
            groups[version]["race_ids"].append(race_id)
            groups[version]["confidence"].append((top_probability, metrics))
        coverages = [
            int(item)
            for item in (args.coverage or "").replace(" ", "").split(",")
            if item
        ]
        report_groups = {}
        for version, group in groups.items():
            report_groups[version] = summarize_rows(group["rows"])
            report_groups[version]["race_ids"] = group["race_ids"]
            report_groups[version]["prediction_run_ids"] = group["run_ids"]
            report_groups[version]["per_race"] = [stripped_row(row) for row in group["rows"]]
            if coverages:
                report_groups[version]["selective_use"] = coverage_slices(
                    group["confidence"], coverages
                )
        report = {
            "kind": "prediction-evaluation-v1",
            "created_at": now_iso(),
            "scored_forecast_kinds": "all" if kinds is None else sorted(kinds),
            "excluded_by_forecast_kind": excluded,
            "snapshot_policy": "latest prediction per race and model version",
            "pending_races": pending,
            "models": report_groups,
        }
        evaluation_id = "ev_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?)",
            (evaluation_id, "evaluate", args.model_version, report["created_at"], canonical(report)),
        )
        report["evaluation_id"] = evaluation_id
    return report


def command_fit(args) -> dict:
    if not VERSION_PATTERN.match(args.model_version):
        raise ValueError("model_version must use 1-64 letters, digits, dots, underscores or hyphens")
    features = [item.strip() for item in args.features.split(",") if item.strip()]
    if not features:
        raise ValueError("At least one feature is required")
    unknown = [name for name in features if not feature_registered(name)]
    if unknown and not args.allow_unknown_features:
        raise ValueError(
            "Unregistered features: " + ", ".join(unknown)
            + ". Register them in scripts/domain.py or pass --allow-unknown-features."
        )
    with connect(args.db) as db:
        if db.execute("SELECT 1 FROM models WHERE model_version = ?", (args.model_version,)).fetchone():
            raise ValueError("model_version already exists; create a new version instead")
        records = load_records(db, train_before=args.train_before)
        if len(records) < args.min_races:
            raise ValueError(f"Need {args.min_races} races before {args.train_before}; found {len(records)}")
        model = fit_model(
            records, features, l2=args.l2, learning_rate=args.learning_rate, epochs=args.epochs
        )
        if model["training"]["race_count"] < args.min_races:
            raise ValueError("Too few usable races after excluding dead heats or incomplete results")
        thin = [
            f"{name} ({stats['coverage']:.2f})"
            for name, stats in model["normalisation"].items()
            if stats["coverage"] < args.min_feature_coverage
        ]
        if thin:
            raise ValueError(
                f"Features below {args.min_feature_coverage:.2f} coverage: " + ", ".join(thin)
            )
        model["feature_definitions"] = describe_features(features)
        blocks = parse_blocks(args.block)
        required_blocks = [name.strip() for name in args.required_block if name.strip()]
        unknown_required = [name for name in required_blocks if name not in blocks]
        if unknown_required:
            raise ValueError(
                "Required blocks were never declared: " + ", ".join(unknown_required)
            )
        if blocks:
            unknown = sorted({name for block in blocks.values() for name in block} - set(features))
            if unknown:
                raise ValueError(f"Blocks reference features that are not trained: {', '.join(unknown)}")
            model["required_blocks"] = required_blocks
            model["feature_blocks"] = blocks
        model.update({"model_version": args.model_version, "created_at": now_iso(), "train_before": args.train_before})
        db.execute(
            "INSERT INTO models VALUES (?, ?, ?, ?)",
            (args.model_version, model["created_at"], model["algorithm"], canonical(model)),
        )
        event_id = "me_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO model_events VALUES (?, ?, ?, ?, ?)",
            (event_id, args.model_version, "fitted", now_iso(), canonical({"train_before": args.train_before})),
        )
    return model


def calibration_window_guard(template: dict | None, records: list[dict], test_indices: list[int]) -> str | None:
    """A calibrated model may only be scored on races after its calibration data.

    ``calibrate`` fits on every post-training race it can see, so scoring an
    earlier race would leak the calibrator's fitting outcomes into the score.
    """
    calibration = (template or {}).get("calibration")
    if not calibration:
        return None
    through = calibration.get("through")
    if not through:
        raise ValueError(
            "Calibrated model has no calibration_through date; rerun calibrate with a current version."
        )
    first_test_date = min(records[index]["card"]["race"]["meeting_date"] for index in test_indices)
    if first_test_date <= through:
        suggested = (date.fromisoformat(through) + timedelta(days=1)).isoformat()
        raise ValueError(
            f"Calibrated models can only be evaluated on races after {through}; "
            f"the test window starts on {first_test_date}. "
            f"Pass --test-after {suggested} or later."
        )
    return through


def walk_forward_predictions(records: list[dict], template: dict | None, test_indices: list[int], refit_every: int):
    """Yield (record, prediction) pairs under an expanding window."""
    cached = None
    next_refit = -1
    for index in test_indices:
        if template is None:
            cached = None
        elif cached is None or index >= next_refit:
            cached = fit_model(records[:index], template["features"], **template["hyperparameters"])
            if template.get("calibration"):
                cached["calibration"] = template["calibration"]
            cached["model_version"] = template.get("model_version", cached.get("model_version"))
            next_refit = index + refit_every
        record = records[index]
        prediction = predict_card(
            record["card"], cached, (template or {}).get("model_version", "market-baseline")
        )
        yield record, prediction


def walk_forward(records: list[dict], template: dict, test_indices: list[int], refit_every: int) -> list[dict]:
    rows = []
    for record, prediction in walk_forward_predictions(records, template, test_indices, refit_every):
        metrics = metric_row(record["card"], record["result"], prediction)
        if metrics:
            rows.append(metrics)
    return rows


def bonferroni_alpha(candidates_in_family: int) -> float:
    if (
        isinstance(candidates_in_family, bool)
        or not isinstance(candidates_in_family, int)
        or candidates_in_family < 1
    ):
        raise ValueError("candidates_in_family must be a positive integer")
    return FAMILYWISE_ALPHA / candidates_in_family


def block_ablation_policy(model: dict) -> tuple[list[str], bool]:
    """Return declared required blocks and whether legacy latest-run gating applies."""
    blocks = model.get("feature_blocks") or {}
    if "required_blocks" not in model:
        return [], bool(blocks)
    required = model.get("required_blocks")
    if not isinstance(required, list) or any(
        not isinstance(name, str) or not name for name in required
    ):
        raise ValueError("Model required_blocks must be a list of non-empty names")
    return list(required), False


def negative_ci_upper(comparison: dict | None, key: str) -> bool:
    """Check a finite, strictly negative upper bound without trusting gate flags."""
    if not comparison:
        return False
    interval = comparison.get(key)
    if not isinstance(interval, list) or len(interval) != 2:
        return False
    upper = interval[1]
    return (
        isinstance(upper, (int, float))
        and not isinstance(upper, bool)
        and math.isfinite(upper)
        and upper < 0
    )


def promotion_eligible(gate: dict) -> bool:
    """Apply every promotion condition, rejecting legacy or stale gate evidence."""
    candidates = gate.get("candidates_in_family")
    policy_valid = (
        isinstance(candidates, int)
        and not isinstance(candidates, bool)
        and candidates >= 1
        and gate.get("multiple_comparison_method") == "bonferroni"
        and gate.get("familywise_alpha") == FAMILYWISE_ALPHA
        and gate.get("bootstrap_resolution_sufficient") is True
    )
    if policy_valid:
        effective_alpha = gate.get("effective_alpha")
        policy_valid = (
            isinstance(effective_alpha, (int, float))
            and not isinstance(effective_alpha, bool)
            and math.isclose(
                effective_alpha,
                bonferroni_alpha(candidates),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
        )
    required = (
        "enough_test_races",
        "logloss_beats_market_baseline",
        "brier_not_worse_than_market",
        "logloss_improvement_ci95_excludes_zero",
        "logloss_improvement_adjusted_ci_excludes_zero",
        "beats_current_champion",
        "multiple_comparison_adjusted",
    )
    eligible = (
        policy_valid
        and gate.get("multiple_comparison_stale") is False
        and all(gate.get(key) is True for key in required)
    )
    if gate.get("champion_comparison_required"):
        eligible = eligible and gate.get("champion_improvement_adjusted_ci_excludes_zero") is True
    if gate.get("calibration_evidence_required"):
        eligible = eligible and gate.get("calibration_evidence_valid") is True
    if gate.get("information_gain_required"):
        eligible = (
            eligible
            and gate.get("information_gain_ci95_excludes_zero") is True
            and gate.get("information_gain_adjusted_ci_excludes_zero") is True
        )
    if gate.get("required_block_ablation_missing"):
        eligible = False
    if gate.get("block_ablation_required"):
        eligible = (
            eligible
            and gate.get("block_ablation_recorded") is True
            and gate.get("block_ablation_ci95_excludes_zero") is True
            and gate.get("block_ablation_adjusted_ci_excludes_zero") is True
        )
    return bool(eligible)


def command_backtest(args) -> dict:
    with connect(args.db) as db:
        template = get_model(db, args.model_version)
        existing_candidates = db.execute(
            "SELECT COUNT(DISTINCT model_version) FROM evaluation_runs WHERE kind = 'backtest'"
        ).fetchone()[0]
        candidate_seen = db.execute(
            "SELECT 1 FROM evaluation_runs WHERE kind = 'backtest' AND model_version = ? LIMIT 1",
            (args.model_version,),
        ).fetchone()
        candidates_in_family = existing_candidates + (0 if candidate_seen else 1)
        effective_alpha = bonferroni_alpha(candidates_in_family)
        records = load_records(db)
        test_indices = []
        for index, record in enumerate(records):
            if index < args.min_train_races:
                continue
            date = record["card"]["race"]["meeting_date"]
            if args.test_after and date < args.test_after:
                continue
            if args.test_before and date >= args.test_before:
                continue
            test_indices.append(index)
        if not test_indices:
            raise ValueError("No races in the walk-forward test window")
        calibration_through = calibration_window_guard(template, records, test_indices)
        candidate_rows = walk_forward(records, template, test_indices, args.refit_every)
        baseline_rows = []
        for index in test_indices:
            record = records[index]
            prediction = predict_card(record["card"], None)
            metrics = metric_row(record["card"], record["result"], prediction)
            if metrics:
                baseline_rows.append(metrics)
        paired = [(c, b) for c, b in zip(candidate_rows, baseline_rows) if c["race_id"] == b["race_id"]]
        candidate_rows = [pair[0] for pair in paired]
        baseline_rows = [pair[1] for pair in paired]
        candidate_summary = summarize_rows(candidate_rows)
        baseline_summary = summarize_rows(baseline_rows)
        comparison = bootstrap_logloss_difference(
            candidate_rows, baseline_rows, samples=args.bootstrap, alpha=effective_alpha
        )
        information_gain = information_gain_vs_market(
            candidate_rows, baseline_rows, samples=args.bootstrap, alpha=effective_alpha
        )

        champion_version = latest_promoted(db)
        champion_summary = None
        champion_comparison = None
        if champion_version and champion_version != args.model_version:
            champion_template = get_model(db, champion_version)
            calibration_window_guard(champion_template, records, test_indices)
            champion_rows = walk_forward(records, champion_template, test_indices, args.refit_every)
            by_race = {row["race_id"]: row for row in champion_rows}
            aligned = [by_race[row["race_id"]] for row in candidate_rows if row["race_id"] in by_race]
            if len(aligned) == len(candidate_rows):
                champion_summary = summarize_rows(aligned)
                champion_comparison = bootstrap_logloss_difference(candidate_rows, aligned, samples=args.bootstrap, alpha=effective_alpha)
            else:
                champion_summary = summarize_rows(champion_rows)

        ablation = None
        if args.ablate:
            blocks = template.get("feature_blocks", {})
            if args.ablate not in blocks:
                raise ValueError(
                    f"Unknown feature block: {args.ablate}. Declare it with fit --block {args.ablate}=..."
                )
            removed = blocks[args.ablate]
            ablated = dict(template)
            ablated["features"] = [name for name in template["features"] if name not in removed]
            if not ablated["features"]:
                raise ValueError("Ablation would leave no features")
            ablated_rows = walk_forward(records, ablated, test_indices, args.refit_every)
            by_race = {row["race_id"]: row for row in ablated_rows}
            aligned = [by_race[row["race_id"]] for row in candidate_rows if row["race_id"] in by_race]
            if len(aligned) == len(candidate_rows):
                ablation = {
                    "block": args.ablate,
                    "features_removed": removed,
                    "without_block": summarize_rows(aligned),
                    "candidate_minus_without_block": bootstrap_logloss_difference(
                        candidate_rows, aligned, samples=args.bootstrap, alpha=effective_alpha
                    ),
                    "interpretation": (
                        "負數代表保留該 block 較好；區間跨 0 即該 block 未證明有時間外價值。"
                    ),
                }
            else:
                ablation = {
                    "block": args.ablate,
                    "features_removed": removed,
                    "without_block": summarize_rows(ablated_rows),
                    "note": "Race alignment incomplete; comparison not computed.",
                }

        enough = len(candidate_rows) >= args.min_test_races
        beats_baseline = (
            candidate_summary.get("logloss", math.inf) < baseline_summary.get("logloss", math.inf)
        )
        brier_not_worse = (
            candidate_summary.get("brier", math.inf) <= baseline_summary.get("brier", math.inf)
        )
        significant = comparison["ci95"][1] is not None and comparison["ci95"][1] < 0
        significant_adjusted = comparison["ci"][1] is not None and comparison["ci"][1] < 0
        champion_comparison_required = bool(champion_version and champion_version != args.model_version)
        champion_significant = True
        champion_adjusted_significant = True
        beats_champion = True
        beats_champion_adjusted = True
        if champion_comparison_required:
            champion_ci_upper = champion_comparison["ci95"][1] if champion_comparison else None
            champion_adjusted_ci_upper = champion_comparison["ci"][1] if champion_comparison else None
            champion_point_better = bool(
                champion_summary
                and champion_summary.get("race_count") == candidate_summary.get("race_count")
                and candidate_summary["logloss"] < champion_summary["logloss"]
            )
            champion_significant = champion_ci_upper is not None and champion_ci_upper < 0
            champion_adjusted_significant = bool(
                champion_adjusted_ci_upper is not None and champion_adjusted_ci_upper < 0
            )
            beats_champion = champion_point_better and champion_significant
            beats_champion_adjusted = champion_point_better and champion_adjusted_significant
        information_gain_positive = bool(
            information_gain.get("delta") is not None and information_gain["delta"] > 0
        )
        information_gain_significant = bool(
            information_gain["ci95"][0] is not None and information_gain["ci95"][0] > 0
        )
        information_gain_adjusted_significant = bool(
            information_gain["ci"][0] is not None and information_gain["ci"][0] > 0
        )
        require_information_gain = bool(getattr(args, "require_information_gain", False))
        required_blocks, legacy_ablation_required = block_ablation_policy(template)
        required_block_ablation_missing = [
            name for name in required_blocks if name != args.ablate
        ]
        block_ablation_required = legacy_ablation_required or bool(
            args.ablate and args.ablate in required_blocks
        )
        block_ablation_recorded = bool(ablation and "candidate_minus_without_block" in ablation)
        ablation_comparison = ablation.get("candidate_minus_without_block") if block_ablation_recorded else None
        block_ablation_significant = negative_ci_upper(ablation_comparison, "ci95")
        block_ablation_adjusted_significant = negative_ci_upper(ablation_comparison, "ci")
        optional_blocks_unproven = (
            [args.ablate]
            if args.ablate
            and not legacy_ablation_required
            and args.ablate not in required_blocks
            and ablation_comparison is not None
            and not block_ablation_adjusted_significant
            else []
        )
        resolution_comparisons = [comparison]
        if champion_comparison is not None:
            resolution_comparisons.append(champion_comparison)
        if require_information_gain:
            resolution_comparisons.append(information_gain)
        if block_ablation_required and ablation_comparison is not None:
            resolution_comparisons.append(ablation_comparison)
        bootstrap_resolution_sufficient = all(
            item.get("ci_resolution_sufficient") is True for item in resolution_comparisons
        )
        bootstrap_resolution_warning = next(
            (item.get("ci_warning") for item in resolution_comparisons if item.get("ci_warning")),
            None,
        )
        if not bootstrap_resolution_sufficient and bootstrap_resolution_warning is None:
            bootstrap_resolution_warning = "Adjusted bootstrap CI evidence is unavailable."
        multiple_comparison_adjusted = bootstrap_resolution_sufficient and all((
            significant_adjusted,
            beats_champion_adjusted,
            information_gain_adjusted_significant if require_information_gain else True,
            block_ablation_adjusted_significant if block_ablation_required else True,
        ))
        gate = {
            "min_test_races_required": args.min_test_races,
            "enough_test_races": enough,
            "logloss_beats_market_baseline": beats_baseline,
            "brier_not_worse_than_market": brier_not_worse,
            "logloss_improvement_ci95_excludes_zero": significant,
            "logloss_improvement_adjusted_ci_excludes_zero": significant_adjusted,
            "champion_comparison_required": champion_comparison_required,
            "champion_improvement_ci95_excludes_zero": champion_significant,
            "champion_improvement_adjusted_ci_excludes_zero": champion_adjusted_significant,
            "beats_current_champion": beats_champion,
            "information_gain_required": require_information_gain,
            "information_gain_positive": information_gain_positive,
            "information_gain_ci95_excludes_zero": information_gain_significant,
            "information_gain_adjusted_ci_excludes_zero": information_gain_adjusted_significant,
            "block_ablation_required": block_ablation_required,
            "required_blocks": required_blocks,
            "required_block_ablation_missing": required_block_ablation_missing,
            "optional_blocks_unproven": optional_blocks_unproven,
            "information_gain_note": (
                "Δf² 係 log loss 差的單調變換（同一個 bootstrap），屬效應量而唔係獨立證據。"
                if require_information_gain
                else None
            ),
            "block_ablation_recorded": block_ablation_recorded,
            "block_ablation_ci95_excludes_zero": block_ablation_significant,
            "block_ablation_adjusted_ci_excludes_zero": block_ablation_adjusted_significant,
            "bootstrap_resolution_sufficient": bootstrap_resolution_sufficient,
            "bootstrap_resolution_warning": bootstrap_resolution_warning,
            "candidates_backtested": candidates_in_family,
            "candidates_in_family": candidates_in_family,
            "familywise_alpha": FAMILYWISE_ALPHA,
            "effective_alpha": effective_alpha,
            "multiple_comparison_method": "bonferroni",
            "multiple_comparison_adjusted": multiple_comparison_adjusted,
            "multiple_comparison_stale": False,
            "multiple_comparison_note": (
                "Bonferroni 按候選模型數調整每個 CI；候選家族改變後必須重跑 backtest。"
            ),
        }
        gate["eligible"] = promotion_eligible(gate)
        report = {
            "kind": "walk-forward-backtest-v1",
            "created_at": now_iso(),
            "model_version": args.model_version,
            "source_model_hash": payload_hash(template),
            "champion_version": champion_version,
            "method": {
                "validation": "expanding-window walk-forward; every fit uses races before the test race only",
                "refit_every_races": args.refit_every,
                "min_train_races": args.min_train_races,
                "test_after": args.test_after,
                "test_before": args.test_before,
                "bootstrap_samples": args.bootstrap,
                "calibration_applied": calibration_through is not None,
                "calibration_through": calibration_through,
            },
            "candidate": candidate_summary,
            "market_baseline": baseline_summary,
            "candidate_minus_baseline_logloss": comparison,
            "information_gain_vs_market": information_gain,
            "champion": champion_summary,
            "candidate_minus_champion_logloss": champion_comparison,
            "ablation": ablation,
            "promotion_gate": gate,
            "per_race": [
                {"candidate": stripped_row(c), "market_baseline": stripped_row(b)}
                for c, b in zip(candidate_rows, baseline_rows)
            ],
        }
        evaluation_id = "ev_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?)",
            (evaluation_id, "backtest", args.model_version, report["created_at"], canonical(report)),
        )
        report["evaluation_id"] = evaluation_id
    return report


def required_ablation_comparison(
    report: dict,
    block: str,
    latest: dict,
    source_model_hash: str,
    current_candidates: int,
    current_alpha: float,
) -> dict | None:
    """Accept only comparable, resolved and independently significant required evidence."""
    context_keys = ("method", "champion_version", "candidate", "market_baseline", "per_race")
    if report.get("source_model_hash") != source_model_hash:
        return None
    if any(report.get(key) != latest.get(key) for key in context_keys):
        return None
    gate = report.get("promotion_gate") or {}
    ablation = report.get("ablation") or {}
    comparison = ablation.get("candidate_minus_without_block")
    stored_alpha = gate.get("effective_alpha")
    comparison_alpha = comparison.get("alpha") if isinstance(comparison, dict) else None
    alpha_matches = (
        isinstance(stored_alpha, (int, float))
        and not isinstance(stored_alpha, bool)
        and math.isclose(stored_alpha, current_alpha, rel_tol=1e-12, abs_tol=1e-15)
        and isinstance(comparison_alpha, (int, float))
        and not isinstance(comparison_alpha, bool)
        and math.isclose(comparison_alpha, current_alpha, rel_tol=1e-12, abs_tol=1e-15)
    )
    if (
        ablation.get("block") != block
        or gate.get("candidates_in_family") != current_candidates
        or not alpha_matches
        or gate.get("multiple_comparison_method") != "bonferroni"
        or gate.get("familywise_alpha") != FAMILYWISE_ALPHA
        or gate.get("multiple_comparison_stale") is not False
        or gate.get("bootstrap_resolution_sufficient") is not True
        or gate.get("block_ablation_required") is not True
        or gate.get("block_ablation_recorded") is not True
        or gate.get("block_ablation_ci95_excludes_zero") is not True
        or gate.get("block_ablation_adjusted_ci_excludes_zero") is not True
        or not isinstance(comparison, dict)
        or comparison.get("ci_resolution_sufficient") is not True
        or not negative_ci_upper(comparison, "ci95")
        or not negative_ci_upper(comparison, "ci")
    ):
        return None
    return comparison


def command_promote(args) -> dict:
    with connect(args.db) as db:
        model = get_model(db, args.model_version)
        rows = db.execute(
            "SELECT * FROM evaluation_runs WHERE kind = 'backtest' AND model_version = ? ORDER BY created_at DESC, rowid DESC",
            (args.model_version,),
        ).fetchall()
        if not rows:
            raise ValueError("Run backtest before promotion")
        report_rows = [(row, json.loads(row["report_json"])) for row in rows]
        row, report = report_rows[0]
        gate = dict(report["promotion_gate"])
        calibration = model.get("calibration") or {}
        calibration_through = calibration.get("through")
        calibration_evidence_required = bool(calibration)
        method = report.get("method") or {}
        gate["calibration_evidence_required"] = calibration_evidence_required
        gate["calibration_evidence_valid"] = (
            not calibration_evidence_required
            or (
                bool(calibration_through)
                and report.get("source_model_hash") == payload_hash(model)
                and method.get("calibration_applied") is True
                and method.get("calibration_through") == calibration_through
            )
        )
        current_candidates = db.execute(
            "SELECT COUNT(DISTINCT model_version) FROM evaluation_runs WHERE kind = 'backtest'"
        ).fetchone()[0]
        current_alpha = bonferroni_alpha(current_candidates)
        stored_alpha = gate.get("effective_alpha")
        alpha_matches = (
            isinstance(stored_alpha, (int, float))
            and not isinstance(stored_alpha, bool)
            and math.isclose(stored_alpha, current_alpha, rel_tol=1e-12, abs_tol=1e-15)
        )
        stale = gate.get("candidates_in_family") != current_candidates or not alpha_matches
        gate["candidates_backtested"] = current_candidates
        gate["current_candidates_in_family"] = current_candidates
        gate["current_effective_alpha"] = current_alpha
        gate["multiple_comparison_stale"] = stale
        gate["multiple_comparison_note"] = (
            "Bonferroni 按候選模型數調整每個 CI；候選家族改變後必須重跑 backtest。"
        )
        if stale:
            gate["multiple_comparison_adjusted"] = False

        required_blocks, legacy_ablation_required = block_ablation_policy(model)
        gate.pop("block_ablation_missing", None)
        if required_blocks:
            source_model_hash = payload_hash(model)
            evidence: dict[str, str] = {}
            evidence_comparisons = []
            for block in required_blocks:
                for evidence_row, evidence_report in report_rows:
                    comparison = required_ablation_comparison(
                        evidence_report, block, report, source_model_hash,
                        current_candidates, current_alpha,
                    )
                    if comparison is not None:
                        evidence[block] = evidence_row["evaluation_id"]
                        evidence_comparisons.append(comparison)
                        break
            missing = [block for block in required_blocks if block not in evidence]
            complete = not missing
            gate["required_block_ablation_missing"] = missing
            gate["required_block_ablation_evidence"] = evidence
            gate["block_ablation_evidence_scope"] = "same-model-hash-and-evaluation-context"
            gate["required_block_ablation_note"] = (
                "逐個 required block 顯著只代表 individual necessity，唔代表 joint necessity。"
            )
            gate["block_ablation_required"] = True
            gate["block_ablation_recorded"] = complete
            gate["block_ablation_ci95_excludes_zero"] = complete
            gate["block_ablation_adjusted_ci_excludes_zero"] = complete
            gate["block_ablation_missing"] = bool(missing)

            resolution_comparisons = [report.get("candidate_minus_baseline_logloss") or {}]
            if gate.get("champion_comparison_required"):
                resolution_comparisons.append(report.get("candidate_minus_champion_logloss") or {})
            if gate.get("information_gain_required"):
                resolution_comparisons.append(report.get("information_gain_vs_market") or {})
            resolution_comparisons.extend(evidence_comparisons)
            resolution_sufficient = all(
                item.get("ci_resolution_sufficient") is True for item in resolution_comparisons
            )
            resolution_warning = next(
                (item.get("ci_warning") for item in resolution_comparisons if item.get("ci_warning")),
                None,
            )
            if not resolution_sufficient and resolution_warning is None:
                resolution_warning = "Adjusted bootstrap CI evidence is unavailable."
            gate["bootstrap_resolution_sufficient"] = resolution_sufficient
            gate["bootstrap_resolution_warning"] = resolution_warning
            champion_adjusted = True
            if gate.get("champion_comparison_required"):
                champion_adjusted = (
                    gate.get("beats_current_champion") is True
                    and gate.get("champion_improvement_adjusted_ci_excludes_zero") is True
                )
            gate["multiple_comparison_adjusted"] = bool(
                not stale
                and resolution_sufficient
                and gate.get("logloss_improvement_adjusted_ci_excludes_zero") is True
                and champion_adjusted
                and (
                    gate.get("information_gain_adjusted_ci_excludes_zero") is True
                    if gate.get("information_gain_required") else True
                )
                and complete
            )
        elif legacy_ablation_required:
            gate["block_ablation_required"] = True
            gate["block_ablation_missing"] = not gate.get("block_ablation_recorded")

        gate["eligible"] = promotion_eligible(gate)
        event_type = "promoted" if gate["eligible"] else "rejected"
        event_id = "me_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO model_events VALUES (?, ?, ?, ?, ?)",
            (
                event_id, args.model_version, event_type, now_iso(),
                canonical({"evaluation_id": row["evaluation_id"], "promotion_gate": gate}),
            ),
        )
    return {"model_version": args.model_version, "event": event_type, "promotion_gate": gate}


def command_value_backtest(args) -> dict:
    with connect(args.db) as db:
        if args.model_version in {"baseline", "market-baseline"}:
            template = None
        else:
            template = get_model(db, args.model_version)
        records = load_records(db)
        test_indices = []
        for index, record in enumerate(records):
            if index < args.min_train_races:
                continue
            date = record["card"]["race"]["meeting_date"]
            if args.test_after and date < args.test_after:
                continue
            if args.test_before and date >= args.test_before:
                continue
            test_indices.append(index)
        if not test_indices:
            raise ValueError("No races in the walk-forward test window")
        calibration_through = calibration_window_guard(template, records, test_indices)
        stream = walk_forward_predictions(records, template, test_indices, args.refit_every)
        settlement = settle_stream(
            stream,
            stake_mode=args.stake,
            cap=args.cap,
            bankroll=args.bankroll,
            min_edge=args.min_edge,
            kelly_multiplier=args.kelly_multiplier,
            max_race_fraction=args.max_race_fraction,
            pool_size=args.pool_size,
            max_pool_fraction=args.max_pool_fraction,
            unit=args.stake_unit,
        )
        report = {
            "kind": "value-backtest-v1",
            "created_at": now_iso(),
            "model_version": args.model_version,
            "method": {
                "validation": "expanding-window walk-forward; bets settled with official dividends when present",
                "min_train_races": args.min_train_races,
                "refit_every_races": args.refit_every,
                "test_after": args.test_after,
                "test_before": args.test_before,
                "pool_size_assumption": args.pool_size,
                "stake_unit": args.stake_unit,
                "calibration_applied": calibration_through is not None,
                "calibration_through": calibration_through,
            },
            "settlement": settlement,
        }
        evaluation_id = "ev_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?)",
            (evaluation_id, "value-backtest", args.model_version, report["created_at"], canonical(report)),
        )
        report["evaluation_id"] = evaluation_id
    return report


def resolve_calibration_method(requested: str, samples: int) -> tuple[str, str]:
    """Resolve ``auto`` to Platt on small samples; isotonic needs enough points."""
    if requested in {"isotonic", "platt"}:
        return requested, "explicit"
    method = "isotonic" if samples >= CALIBRATION_ISOTONIC_MIN_SAMPLES else "platt"
    return method, f"auto(samples={samples}, threshold={CALIBRATION_ISOTONIC_MIN_SAMPLES})"


def calibration_ece_guard(before: float, after: float, *, allow_worse: bool) -> None:
    """Refuse to store a calibrator that makes the holdout worse than raw scores."""
    if allow_worse:
        return
    if after > before + 1e-12:
        raise ValueError(
            f"Calibration made holdout ECE worse ({before:.4f} -> {after:.4f}). "
            "Store it anyway with --allow-worse-calibration, or use --method platt / more samples."
        )


def prediction_calibration_points(record: dict, prediction: dict) -> list[tuple[float, int]]:
    """Flatten one race's probabilities without losing race-grouped normalisation."""
    winners = {
        int(row["horse_no"])
        for row in record["result"]["results"]
        if row.get("finish_position") == 1
    }
    return [
        (float(item["win_probability"]), 1 if int(item["horse_no"]) in winners else 0)
        for item in prediction["runners"]
    ]


def command_calibrate(args) -> dict:
    with connect(args.db) as db:
        source = get_model(db, args.model_version)
        if db.execute("SELECT 1 FROM models WHERE model_version = ?", (args.new_version,)).fetchone():
            raise ValueError("new_version already exists; pick a fresh calibration version")
        train_before = source.get("train_before")
        if not train_before:
            raise ValueError("Source model has no train_before boundary for post-training calibration")
        records = load_records(db)
        pre_records = [
            record for record in records
            if record["card"]["race"]["meeting_date"] < train_before
        ]
        post_records = [
            record for record in records
            if record["card"]["race"]["meeting_date"] >= train_before
        ]
        if len(pre_records) < args.min_train_races:
            raise ValueError(
                f"Need {args.min_train_races} completed source-training races before {train_before}; "
                f"found {len(pre_records)}"
            )
        if len(post_records) < 2:
            raise ValueError(
                f"Need at least 2 completed races on or after {train_before} for calibration fitting "
                f"and a later evaluation holdout; found {len(post_records)}; "
                f"add at least {2 - len(post_records)} more post-training races"
            )

        raw_source = dict(source)
        raw_source.pop("calibration", None)
        post_predictions = [
            (record, predict_card(record["card"], raw_source, args.model_version))
            for record in post_records
        ]
        evaluation_count = max(1, math.ceil(len(post_predictions) * 0.25))
        fit_predictions = post_predictions[:-evaluation_count]
        evaluation_predictions = post_predictions[-evaluation_count:]
        fit_points = [
            point
            for record, prediction in fit_predictions
            for point in prediction_calibration_points(record, prediction)
        ]
        if len(fit_points) < args.min_samples:
            raise ValueError(
                f"Need at least {args.min_samples} post-training fit runner predictions; "
                f"found {len(fit_points)} ({args.min_samples - len(fit_points)} more needed) "
                f"across {len(fit_predictions)} fit races and "
                f"{len(evaluation_predictions)} evaluation races from "
                f"{len(post_predictions)} post-training races. "
                "At least 1 more completed post-training race is needed; "
                "the exact additional race count depends on future field sizes, "
                "and the latest 25% of post-training races remain reserved for ECE evaluation"
            )

        scores = [point[0] for point in fit_points]
        labels = [point[1] for point in fit_points]
        method, method_policy = resolve_calibration_method(args.method, len(fit_points))
        fitted = isotonic_fit(scores, labels) if method == "isotonic" else platt_fit(scores, labels)
        calibration_through = max(
            record["card"]["race"]["meeting_date"] for record, _ in post_predictions
        )
        fit_race_ids = [record["card"]["race"]["race_id"] for record, _ in fit_predictions]
        evaluation_race_ids = [
            record["card"]["race"]["race_id"] for record, _ in evaluation_predictions
        ]
        model = dict(source)
        model.update(
            {
                "model_version": args.new_version,
                "created_at": now_iso(),
                "calibrated_from": args.model_version,
                "calibration": {
                    "method": method,
                    "method_policy": method_policy,
                    "model": fitted,
                    "samples": len(fit_points),
                    "evaluation_samples": 0,
                    "post_train_samples": len(fit_points),
                    "source": "post-train-out-of-sample",
                    "train_before": train_before,
                    "through": calibration_through,
                    "fit_race_count": len(fit_predictions),
                    "evaluation_race_count": len(evaluation_predictions),
                    "post_train_race_count": len(post_predictions),
                    "fit_race_ids": fit_race_ids,
                    "evaluation_race_ids": evaluation_race_ids,
                    "fit_period": {
                        "first_meeting_date": fit_predictions[0][0]["card"]["race"]["meeting_date"],
                        "last_meeting_date": fit_predictions[-1][0]["card"]["race"]["meeting_date"],
                    },
                    "evaluation_period": {
                        "first_meeting_date": evaluation_predictions[0][0]["card"]["race"]["meeting_date"],
                        "last_meeting_date": evaluation_predictions[-1][0]["card"]["race"]["meeting_date"],
                    },
                },
            }
        )

        before_points: list[tuple[float, int]] = []
        after_points: list[tuple[float, int]] = []
        for record, prediction in evaluation_predictions:
            race_points = prediction_calibration_points(record, prediction)
            before_points.extend(race_points)
            adjusted = apply_calibration(model, [point[0] for point in race_points])
            after_points.extend(
                (probability, point[1])
                for probability, point in zip(adjusted, race_points)
            )
        model["calibration"]["evaluation_samples"] = len(before_points)
        model["calibration"]["post_train_samples"] = len(fit_points) + len(before_points)
        ece_before = expected_calibration_error(before_points)
        ece_after = expected_calibration_error(after_points)
        calibration_ece_guard(ece_before, ece_after, allow_worse=args.allow_worse_calibration)
        model["calibration"]["ece_before"] = ece_before
        model["calibration"]["ece_after"] = ece_after
        model["calibration"]["ece_delta"] = round(ece_after - ece_before, 6)
        model["calibration"]["accepted"] = True
        db.execute(
            "INSERT INTO models VALUES (?, ?, ?, ?)",
            (args.new_version, model["created_at"], model["algorithm"], canonical(model)),
        )
        event_id = "me_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO model_events VALUES (?, ?, ?, ?, ?)",
            (
                event_id, args.new_version, "calibrated", now_iso(),
                canonical({
                    "calibrated_from": args.model_version,
                    "method": method,
                    "samples": len(fit_points),
                    "evaluation_samples": len(before_points),
                    "fit_race_ids": fit_race_ids,
                    "evaluation_race_ids": evaluation_race_ids,
                }),
            ),
        )
    return model["calibration"] | {"model_version": args.new_version}


def command_calibration_drift(args) -> dict:
    with connect(args.db) as db:
        kinds = {item.strip() for item in args.kinds.split(",") if item.strip()}
        rows = db.execute(
            "SELECT * FROM prediction_runs ORDER BY created_at, rowid"
        ).fetchall()
        selected: dict[tuple[str, str], object] = {}
        for row in rows:
            if kinds and row["forecast_kind"] not in kinds:
                continue
            if args.model_version != "all" and row["model_version"] != args.model_version:
                continue
            selected[(row["model_version"], row["race_id"])] = row

        grouped: dict[str, dict[str, list[tuple[float, int]]]] = {}
        for (version, race_id), row in selected.items():
            result = latest_result(db, race_id)
            if not result:
                continue
            card, _ = latest_card(db, race_id, row["snapshot_id"])
            prediction = json.loads(row["payload_json"])
            period = period_of(card["race"]["meeting_date"], mode=args.period)
            points = prediction_calibration_points({"card": card, "result": result}, prediction)
            grouped.setdefault(version, {}).setdefault(period, []).extend(points)

        models = {}
        for version, by_period in grouped.items():
            model = None
            if version not in {"market-baseline", "baseline"}:
                try:
                    model = get_model(db, version)
                except ValueError:
                    model = None
            calibration = (model or {}).get("calibration") or {}
            baseline = calibration.get("ece_after")
            summary = summarize_periods(
                by_period,
                baseline_ece=baseline,
                min_samples=args.min_samples,
                threshold=args.threshold,
            )
            summary["baseline_source"] = (
                "calibration.ece_after" if baseline is not None else "none"
            )
            models[version] = summary

        report = {
            "kind": "calibration-drift-v1",
            "created_at": now_iso(),
            "period": args.period,
            "kinds": sorted(kinds) if kinds else "all",
            "models": models,
            "drift_detected": any(item["drift_detected"] for item in models.values()),
        }
        evaluation_id = "ev_" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?)",
            (evaluation_id, "calibration-drift", args.model_version, report["created_at"], canonical(report)),
        )
        report["evaluation_id"] = evaluation_id
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialise the append-only SQLite store")
    init.add_argument("--db", required=True)

    for name, help_text in [("import-card", "Append a validated race-card snapshot"), ("import-result", "Append an official result snapshot")]:
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--db", required=True)
        command.add_argument("--file", required=True)
        if name == "import-card":
            command.add_argument(
                "--allow-unknown-features", action="store_true",
                help="Accept feature names missing from scripts/domain.py (turns off the leakage guard)",
            )

    odds = sub.add_parser("import-odds", help="Append a new card snapshot with updated odds")
    odds.add_argument("--db", required=True)
    odds.add_argument("--file", required=True)
    odds.add_argument("--race-id")

    diagnose = sub.add_parser("diagnose-card", help="Report card completeness before predicting")
    diagnose.add_argument("--db", required=True)
    diagnose.add_argument("--race-id")

    predict = sub.add_parser("predict", help="Freeze an append-only race prediction")
    predict.add_argument("--db", required=True)
    predict.add_argument("--race-id", required=True)
    predict.add_argument("--snapshot-id")
    predict.add_argument("--model-version", default="auto", help="auto, baseline, or a fitted version")
    predict.add_argument("--historical", action="store_true", help="Label a reconstruction; never use after results are imported")
    predict.add_argument("--output")

    evaluate = sub.add_parser("evaluate", help="Evaluate frozen predictions against official results")
    evaluate.add_argument("--db", required=True)
    evaluate.add_argument("--model-version")
    evaluate.add_argument(
        "--kinds", default="pre-race",
        help="Comma-separated forecast kinds to score; default is live pre-race forecasts only",
    )
    evaluate.add_argument(
        "--all-kinds", action="store_true",
        help="Score every forecast kind together (legacy behaviour, not recommended)",
    )
    evaluate.add_argument(
        "--coverage",
        help="Comma-separated confidence bands (percent) for selective-use reporting, e.g. 5,10,20,50",
    )
    evaluate.add_argument("--output")

    fit = sub.add_parser("fit", help="Fit a candidate conditional-logit model")
    fit.add_argument("--db", required=True)
    fit.add_argument("--model-version", required=True)
    fit.add_argument("--features", default="market_logp,intent_centered")
    fit.add_argument("--train-before", required=True, help="Only races with meeting_date before YYYY-MM-DD")
    fit.add_argument("--min-races", type=int, default=30)
    fit.add_argument("--l2", type=float, default=0.01)
    fit.add_argument("--learning-rate", type=float, default=0.05)
    fit.add_argument("--epochs", type=int, default=300)
    fit.add_argument(
        "--required-block", action="append", default=[], metavar="NAME",
        help="Block that must prove itself in --ablate before promotion (others are optional)",
    )
    fit.add_argument(
        "--min-feature-coverage", type=float, default=0.5,
        help="Reject training when a feature is observed for less than this share of runners",
    )
    fit.add_argument(
        "--allow-unknown-features", action="store_true",
        help="Train on feature names missing from scripts/domain.py (turns off the leakage guard)",
    )
    fit.add_argument(
        "--block", action="append", default=[], metavar="NAME=FEATURES",
        help="Declare a named feature block for later ablation, e.g. --block intent=intent_centered",
    )
    fit.add_argument("--output")

    backtest = sub.add_parser("backtest", help="Run expanding-window out-of-sample tests")
    backtest.add_argument("--db", required=True)
    backtest.add_argument("--model-version", required=True)
    backtest.add_argument("--min-train-races", type=int, default=30)
    backtest.add_argument("--min-test-races", type=int, default=50)
    backtest.add_argument(
        "--require-information-gain",
        action="store_true",
        help="gate 加一條：Δf² 必須 > 0（Benter 1994），預設關閉以保持舊行為",
    )
    backtest.add_argument("--refit-every", type=int, default=5)
    backtest.add_argument("--test-after", help="Inclusive YYYY-MM-DD")
    backtest.add_argument("--test-before", help="Exclusive YYYY-MM-DD")
    backtest.add_argument("--bootstrap", type=int, default=1000)
    backtest.add_argument("--ablate", help="Feature block name to drop in a paired ablation run")
    backtest.add_argument("--output")

    value = sub.add_parser(
        "value-backtest", help="Settle walk-forward bets against official dividends"
    )
    value.add_argument("--db", required=True)
    value.add_argument("--model-version", required=True)
    value.add_argument("--stake", choices=["flat", "kelly"], default="flat")
    value.add_argument("--cap", type=float, default=0.01, help="Max share of bankroll per bet")
    value.add_argument("--bankroll", type=float, default=10000.0)
    value.add_argument("--min-edge", type=float, default=0.0)
    value.add_argument("--kelly-multiplier", type=float, default=0.25)
    value.add_argument("--max-race-fraction", type=float, default=0.05)
    value.add_argument(
        "--pool-size", type=float,
        help="Optional win-pool size assumption applied to each backtest race",
    )
    value.add_argument("--max-pool-fraction", type=float, default=0.001)
    value.add_argument("--stake-unit", type=float, default=10.0)
    value.add_argument("--min-train-races", type=int, default=30)
    value.add_argument("--refit-every", type=int, default=5)
    value.add_argument("--test-after", help="Inclusive YYYY-MM-DD")
    value.add_argument("--test-before", help="Exclusive YYYY-MM-DD")
    value.add_argument("--output")

    calibrate = sub.add_parser(
        "calibrate",
        help="Fit post-training calibration and evaluate it on a later holdout",
    )
    calibrate.add_argument("--db", required=True)
    calibrate.add_argument("--model-version", required=True)
    calibrate.add_argument("--new-version", required=True)
    calibrate.add_argument(
        "--method", choices=["auto", "isotonic", "platt"], default="auto",
        help="auto uses isotonic only with at least 500 fit points, else Platt",
    )
    calibrate.add_argument(
        "--allow-worse-calibration", action="store_true",
        help="Store the calibrator even when it makes holdout ECE worse",
    )
    calibrate.add_argument("--min-train-races", type=int, default=50)
    calibrate.add_argument("--min-samples", type=int, default=200)
    calibrate.add_argument("--output")

    drift = sub.add_parser(
        "calibration-drift", help="Per-period ECE against the calibration baseline"
    )
    drift.add_argument("--db", required=True)
    drift.add_argument("--model-version", default="all")
    drift.add_argument("--period", choices=["month", "meeting"], default="month")
    drift.add_argument("--kinds", default="pre-race")
    drift.add_argument("--min-samples", type=int, default=100)
    drift.add_argument("--threshold", type=float, default=0.02)
    drift.add_argument("--output")

    promote = sub.add_parser("promote", help="Promote only an eligible model; otherwise record rejection")
    promote.add_argument("--db", required=True)
    promote.add_argument("--model-version", required=True)
    return parser


def configure_stdio() -> None:
    """Windows consoles default to cp950; the report is full of Chinese text."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            init_db(args.db)
            result = {"database": str(Path(args.db).resolve()), "status": "ready"}
            write_or_print(result, None)
            return 0
        init_db(args.db)
        handlers = {
            "import-card": command_import_card,
            "import-odds": command_import_odds,
            "import-result": command_import_result,
            "diagnose-card": command_diagnose_card,
            "predict": command_predict,
            "evaluate": command_evaluate,
            "fit": command_fit,
            "backtest": command_backtest,
            "value-backtest": command_value_backtest,
            "calibrate": command_calibrate,
            "calibration-drift": command_calibration_drift,
            "promote": command_promote,
        }
        result = handlers[args.command](args)
        summary = None
        if getattr(args, "output", None):
            summary = {"command": args.command, "status": "ok"}
            if args.command == "backtest":
                summary["promotion_gate"] = result["promotion_gate"]
            if args.command == "fit":
                summary["training"] = result["training"]
        write_or_print(result, getattr(args, "output", None), summary)
        return 0
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
