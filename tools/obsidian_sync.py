#!/usr/bin/env python3
"""Obsidian <-> racing-data bridge for predict-hk-racing.

Three verbs:

  export  --db DB --vault DIR     write one Obsidian note per race from the store
  import  --vault DIR --out DIR   read canonical notes and write card/result JSON
  migrate --vault DIR --out DIR   read legacy "Picks Summary" notes and write
                                  *partial* card/result JSON plus a report

Canonical note shape (what ``export`` writes and ``import`` reads):

    ---
    race_id: 2026-06-24-HV-R1
    meeting_date: 2026-06-24
    venue: HV
    race_no: 1
    off_time: 2026-06-24T19:15:00+08:00
    distance_m: 2200
    place_slots: 3
    collection_mode: historical
    ---

    ## Runners
    | No | Horse | Jockey | Trainer | Draw | Weight | Rating | Odds | Scratched |
    |----|-------|--------|---------|------|--------|--------|------|-----------|

    ## Result
    | No | Finish | Dividend |

Legacy notes (the Hermes "Picks Summary" tables) only carry the four picks,
hit markers and medals, so ``migrate`` marks every card ``partial: true`` and
lists what is missing instead of pretending the field is complete.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VENUE_CODES = {
    "happy valley": "HV", "跑馬地": "HV", "hv": "HV",
    "sha tin": "ST", "沙田": "ST", "st": "ST",
    "goodwood": "GD", "gd": "GD",
}
MEDALS = {"🥇": 1, "🥈": 2, "🥉": 3}
PICK_RE = re.compile(r"#?(\d+)\s*(.*)")
MARKERS = "✅🥇🥈🥉❌"

CARD_REQUIRED_RACE_FIELDS = ("race_id", "meeting_date", "venue", "race_no", "off_time", "distance_m")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip().splitlines()
    meta: dict[str, str] = {}
    for line in raw:
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 4:]


def tables(text: str) -> list[list[list[str]]]:
    """Return every markdown table as a list of rows of cells."""
    found: list[list[list[str]]] = []
    rows: list[list[str]] = []

    def flush() -> None:
        nonlocal rows
        if len(rows) >= 2:
            found.append(rows)
        rows = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(set(cell) <= set("-: ") for cell in cells) and cells:
                continue  # separator row
            rows.append(cells)
        else:
            flush()
    flush()
    return found


def clean_name(value: str) -> str:
    text = value
    for medal in MEDALS:
        text = text.replace(medal, "")
    text = text.replace("✅", "").replace("❌", "")
    return text.strip(" -_·*")


def parse_pick(cell: str) -> tuple[int, str, int | None] | None:
    cell = cell.strip()
    if not cell or cell.upper() == "N/A":
        return None
    match = PICK_RE.match(cell)
    if not match:
        return None
    number = int(match.group(1))
    rest = match.group(2)
    position = next((MEDALS[medal] for medal in MEDALS if medal in rest), None)
    return number, clean_name(rest) or f"H{number}", position


def venue_code(value: str) -> str:
    key = value.strip().lower()
    if key in VENUE_CODES:
        return VENUE_CODES[key]
    return "".join(ch for ch in value.upper() if ch.isalnum())[:3] or "UNK"


def legacy_note_to_records(path: Path) -> tuple[list[dict], list[dict], dict]:
    """Turn a legacy Hermes picks note into partial cards + partial results."""
    text = read_text(path)
    meta, body = parse_frontmatter(text)
    date = meta.get("date", "")
    venue = meta.get("venue", "")
    code = venue_code(venue)
    cards: list[dict] = []
    results: list[dict] = []
    stats = {"races": 0, "cards": 0, "results": 0, "no_picks": 0}

    for table in tables(body):
        header = [cell.lower() for cell in table[0]]
        if not any("race" == cell for cell in header) or not any("pick" in cell for cell in header):
            continue
        pick_indexes = [i for i, cell in enumerate(header) if "pick" in cell]
        for row in table[1:]:
            if len(row) <= max(pick_indexes):
                continue
            race_label = row[0].strip()
            match = re.search(r"(\d+)", race_label)
            if not match:
                continue
            race_no = int(match.group(1))
            picks = [parse_pick(row[i]) for i in pick_indexes if i < len(row)]
            picks = [pick for pick in picks if pick]
            stats["races"] += 1
            if not picks:
                stats["no_picks"] += 1
                continue
            distance_raw = row[1] if len(row) > 1 else ""
            distance = int(re.sub(r"[^0-9]", "", distance_raw) or 0)
            race_id = f"{date}-{code}-R{race_no}"
            card = {
                "schema_version": "1.1",
                "collection_mode": "historical",
                "partial": True,
                "captured_at": f"{date}T09:00:00+08:00",
                "data_cutoff": f"{date}T09:00:00+08:00",
                "race": {
                    "race_id": race_id,
                    "meeting_date": date,
                    "venue": code,
                    "race_no": race_no,
                    "off_time": f"{date}T19:15:00+08:00",
                    "distance_m": distance or 1200,
                    "place_slots": 3,
                    "declared_starters": len(picks),
                },
                "sources": [{
                    "source_id": "S1",
                    "title": f"Obsidian: {path.stem}",
                    "url": path.as_uri(),
                    "retrieved_at": f"{date}T09:00:00+08:00",
                    "official": False,
                }],
                "runners": [
                    {
                        "horse_no": number,
                        "horse_name": name,
                        "scratched": False,
                        "features": {},
                        "intent_evidence": [],
                    }
                    for number, name, _ in picks
                ],
                "missing": ["odds", "full_field", "sectionals", "intent_evidence"],
            }
            cards.append(card)
            finished = [
                {"horse_no": number, "finish_position": position, "status": "OK", "win_dividend": None}
                for number, _, position in picks
                if position
            ]
            seen = set()
            unique = []
            for row_result in sorted(finished, key=lambda item: item["finish_position"]):
                if row_result["finish_position"] in seen:
                    continue
                seen.add(row_result["finish_position"])
                unique.append(row_result)
            if unique and any(item["finish_position"] == 1 for item in unique):
                results.append({
                    "schema_version": "1.0",
                    "race_id": race_id,
                    "recorded_at": f"{date}T23:59:00+08:00",
                    "sources": [{"source_id": "R1", "title": f"Obsidian medals: {path.stem}",
                                 "url": path.as_uri()}],
                    "results": unique,
                    "partial": True,
                    "missing": ["full_result", "dividends"],
                })
                stats["results"] += 1
    stats["cards"] = len(cards)
    return cards, results, stats


def canonical_note_to_records(path: Path) -> tuple[dict | None, dict | None, list[str]]:
    text = read_text(path)
    meta, body = parse_frontmatter(text)
    missing = [field for field in CARD_REQUIRED_RACE_FIELDS if not meta.get(field)]
    if missing:
        return None, None, [f"missing frontmatter: {', '.join(missing)}"]
    race = {
        "race_id": meta["race_id"],
        "meeting_date": meta["meeting_date"],
        "venue": meta["venue"],
        "race_no": int(meta["race_no"]),
        "off_time": meta["off_time"],
        "distance_m": int(meta["distance_m"]),
        "place_slots": int(meta.get("place_slots", 3)),
    }
    runners = []
    result_rows = []
    for table in tables(body):
        header = [cell.lower() for cell in table[0]]
        if "horse" in header:
            for row in table[1:]:
                if len(row) < 2:
                    continue
                runners.append({
                    "horse_no": int(row[0]),
                    "horse_name": row[1],
                    "jockey": row[2] if len(row) > 2 else "",
                    "trainer": row[3] if len(row) > 3 else "",
                    "draw": int(row[4]) if len(row) > 4 and row[4].isdigit() else None,
                    "weight_lb": float(row[5]) if len(row) > 5 and row[5] else None,
                    "rating": float(row[6]) if len(row) > 6 and row[6] else None,
                    "win_odds": float(row[7]) if len(row) > 7 and row[7] else None,
                    "scratched": (row[8].strip().lower() in {"yes", "true", "1"}) if len(row) > 8 else False,
                    "features": {},
                    "intent_evidence": [],
                })
        elif "finish" in header:
            for row in table[1:]:
                if len(row) < 2 or not row[1]:
                    continue
                result_rows.append({
                    "horse_no": int(row[0]),
                    "finish_position": int(row[1]),
                    "status": "OK",
                    "win_dividend": float(row[2]) if len(row) > 2 and row[2] else None,
                })
    if len(runners) < 2:
        return None, None, ["fewer than two runners"]
    card = {
        "schema_version": "1.1",
        "collection_mode": meta.get("collection_mode", "historical"),
        "captured_at": meta.get("captured_at") or f"{race['meeting_date']}T09:00:00+08:00",
        "data_cutoff": meta.get("data_cutoff") or f"{race['meeting_date']}T09:00:00+08:00",
        "race": race,
        "sources": [{"source_id": "S1", "title": f"Obsidian: {path.stem}",
                     "url": path.as_uri(),
                     "retrieved_at": f"{race['meeting_date']}T09:00:00+08:00"}],
        "runners": [
            {key: value for key, value in runner.items() if value is not None}
            for runner in runners
        ],
    }
    result = None
    if result_rows:
        result = {
            "schema_version": "1.0",
            "race_id": race["race_id"],
            "recorded_at": f"{race['meeting_date']}T23:59:00+08:00",
            "sources": [{"source_id": "R1", "title": f"Obsidian: {path.stem}",
                         "url": path.as_uri()}],
            "results": result_rows,
        }
    return card, result, []


def note_for_card(card: dict, result: dict | None) -> str:
    race = card["race"]
    lines = [
        "---",
        f"race_id: {race['race_id']}",
        f"meeting_date: {race['meeting_date']}",
        f"venue: {race['venue']}",
        f"race_no: {race['race_no']}",
        f"off_time: {race['off_time']}",
        f"distance_m: {race['distance_m']}",
        f"place_slots: {race.get('place_slots', 3)}",
        f"collection_mode: {card.get('collection_mode', 'historical')}",
        "---",
        "",
        f"# {race['race_id']}",
        "",
        "## Runners",
        "| No | Horse | Jockey | Trainer | Draw | Weight | Rating | Odds | Scratched |",
        "|----|-------|--------|---------|------|--------|--------|------|-----------|",
    ]
    for runner in card.get("runners", []):
        lines.append(
            "| {no} | {name} | {jockey} | {trainer} | {draw} | {weight} | {rating} | {odds} | {scr} |".format(
                no=runner.get("horse_no", ""),
                name=runner.get("horse_name", ""),
                jockey=runner.get("jockey", ""),
                trainer=runner.get("trainer", ""),
                draw=runner.get("draw", ""),
                weight=runner.get("weight_lb", ""),
                rating=runner.get("rating", ""),
                odds=runner.get("win_odds", ""),
                scr="yes" if runner.get("scratched") else "no",
            )
        )
    lines += ["", "## Result", "| No | Finish | Dividend |", "|----|--------|----------|"]
    for row in (result or {}).get("results", []):
        lines.append(f"| {row.get('horse_no')} | {row.get('finish_position')} | {row.get('win_dividend') or ''} |")
    return "\n".join(lines) + "\n"


def command_export(args) -> int:
    import sqlite3

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    vault = Path(args.vault)
    vault.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in db.execute("SELECT payload_json FROM card_snapshots ORDER BY race_id, captured_at"):
        card = json.loads(row["payload_json"])
        race = card["race"]
        result_row = db.execute(
            "SELECT payload_json FROM result_snapshots WHERE race_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
            (race["race_id"],),
        ).fetchone()
        result = json.loads(result_row["payload_json"]) if result_row else None
        target = vault / f"{race['meeting_date']} {race['venue']} R{race['race_no']}.md"
        target.write_text(note_for_card(card, result), encoding="utf-8")
        written += 1
    print(json.dumps({"status": "ok", "notes": written, "vault": str(vault)}, ensure_ascii=False))
    return 0


def _write_records(out: Path, cards: list[dict], results: list[dict]) -> None:
    (out / "cards").mkdir(parents=True, exist_ok=True)
    (out / "results").mkdir(parents=True, exist_ok=True)
    for card in cards:
        (out / "cards" / f"{card['race']['race_id']}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    for result in results:
        (out / "results" / f"{result['race_id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def command_import(args) -> int:
    vault = Path(args.vault)
    out = Path(args.out)
    cards, results, errors = [], [], []
    for path in sorted(vault.rglob("*.md")):
        card, result, problems = canonical_note_to_records(path)
        if problems:
            errors.append({"note": str(path), "problems": problems})
            continue
        cards.append(card)
        if result:
            results.append(result)
    _write_records(out, cards, results)
    print(json.dumps({"status": "ok", "cards": len(cards), "results": len(results),
                      "skipped": errors}, ensure_ascii=False, indent=2))
    return 0


def command_migrate(args) -> int:
    vault = Path(args.vault)
    out = Path(args.out)
    cards, results, report = [], [], []
    for path in sorted(vault.rglob("*.md")):
        note_cards, note_results, stats = legacy_note_to_records(path)
        if not note_cards:
            continue
        cards.extend(note_cards)
        results.extend(note_results)
        report.append({"note": str(path), **stats})
    _write_records(out, cards, results)
    (out / "migration-report.json").write_text(
        json.dumps({"notes": report, "cards": len(cards), "results": len(results)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "cards": len(cards), "results": len(results),
                      "notes": len(report), "out": str(out)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Write one Obsidian note per stored race")
    export.add_argument("--db", required=True)
    export.add_argument("--vault", required=True)
    imp = sub.add_parser("import", help="Read canonical notes into card/result JSON")
    imp.add_argument("--vault", required=True)
    imp.add_argument("--out", required=True)
    mig = sub.add_parser("migrate", help="Read legacy picks notes into partial JSON")
    mig.add_argument("--vault", required=True)
    mig.add_argument("--out", required=True)
    return parser


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        if args.command == "export":
            return command_export(args)
        if args.command == "import":
            return command_import(args)
        return command_migrate(args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
