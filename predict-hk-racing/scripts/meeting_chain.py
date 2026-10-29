#!/usr/bin/env python3
"""Build and verify an append-only, point-in-time chain for one HK race meeting."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from exotic import parse_pair_text
from racing_lab import (
    command_import_card,
    command_import_result,
    connect,
    init_db,
    parse_ts,
    validate_card,
    validate_result,
)

HKT = timezone(timedelta(hours=8))
SCRATCH_CODES = {"WV", "WX", "W", "W-NR", "W(V)", "W(X)"}
NON_FINISH_CODES = {"DNF", "PU", "UR", "TNP", "D", "F", "V"}
CLASS_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
RUNNER_RE = re.compile(
    r"^(?P<no>\d+)\s+(?P<flags>(?:(?:F|↑|↓|T)\s+)*)"
    r"(?P<name>\S+)\s+(?P<horse_id>[A-Z]\d+)\s*"
    r"(?:\((?P<gear>[^)]*)\)\s*)?(?P<trainer>\S+)\s+"
    r"(?P<minimum_weight>#\s*)?(?P<weight_lb>\d+)(?:\s+\?\+(?P<probable_overweight_lb>\d+))?"
    r"(?:\s+(?P<jockey>\S+)\s+\((?P<draw>\d+)\))?(?:\s|$)"
)
RESERVE_RE = re.compile(
    r"^(?P<reserve_no>S\d+)\s+(?P<name>\S+)\s+(?P<horse_id>[A-Z]\d+)\s*"
    r"(?:\((?P<gear>[^)]*)\)\s*)?(?P<trainer>\S+)\s+(?P<weight_lb>\d+)(?:\s|$)"
)


def now_hk() -> str:
    return datetime.now(HKT).isoformat(timespec="milliseconds")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def horse_id(value: str) -> str:
    match = re.fullmatch(r"([A-Z])(\d+)", value)
    if not match:
        raise ValueError(f"Invalid horse id: {value}")
    return match.group(1) + match.group(2).zfill(3)


def integer(value: str):
    return int(value) if re.fullmatch(r"\d+", value.strip()) else None


def number(value: str):
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def extract_pdf_pages(pdf_path: Path) -> list[dict]:
    import pdfplumber

    with pdfplumber.open(str(pdf_path)) as pdf:
        return [{"page": index, "text": page.extract_text() or ""} for index, page in enumerate(pdf.pages, 1)]


def find_race_starts(pages: list[dict], race_count: int) -> list[tuple[int, int]]:
    starts = []
    for page_index, page in enumerate(pages):
        lines = page["text"].splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            compact_header = re.match(r"^([0-9]{1,2})\s+下午", stripped)
            if compact_header and index + 2 < len(lines) and re.match(r"^\d{1,2}\.\d{2} [AP]\.M\.", lines[index + 1].strip()):
                starts.append((int(compact_header.group(1)), page_index))
                continue
            if not re.fullmatch(r"[0-9]{1,2}", stripped) or not 1 <= int(stripped) <= race_count or index + 2 >= len(lines):
                continue
            if re.match(r"^\d{1,2}\.\d{2} [AP]\.M\.", lines[index + 2].strip()) and "下午" in lines[index + 1]:
                starts.append((int(stripped), page_index))
    expected = list(range(1, race_count + 1))
    if [race for race, _ in starts] != expected:
        raise ValueError(f"PDF race starts {starts!r} do not match {expected}")
    return starts


def rating_from_block(block: list[dict]) -> tuple[int, str]:
    for entry in block[:5]:
        line = entry["line"]
        if re.search(r"\([A-Z]{2,4}\)\s+\d+\s+[a-z/]+\s+[a-z]\s+", line):
            match = re.search(r"\((\d{1,3})\)", line)
            if match:
                return int(match.group(1)), line
    raise ValueError(f"No rating/detail line in runner block: {block[0]['line']}")


def parse_runner_block(block: list[dict], reserve: bool = False) -> dict:
    first = block[0]
    match = (RESERVE_RE if reserve else RUNNER_RE).match(first["line"].strip())
    if not match:
        raise ValueError(f"Cannot parse runner header: {first['line']}")
    values = match.groupdict()
    rating, detail_quote = rating_from_block(block)
    scratched = not reserve and any("退出" in entry["line"] for entry in block[:9])
    if not reserve and not scratched and (not values.get("jockey") or not values.get("draw")):
        raise ValueError(f"Active runner lacks jockey/draw: {first['line']}")
    result = {
        "horse_name": values["name"],
        "horse_id": horse_id(values["horse_id"]),
        "jockey": values.get("jockey"),
        "trainer": values["trainer"],
        "draw": int(values["draw"]) if values.get("draw") else None,
        "weight_lb": int(values["weight_lb"]),
        "rating": rating,
        "body_weight_lb": None,
        "gear": values.get("gear") or "",
        "scratched": scratched,
        "source_refs": [{
            "source_id": "PDF",
            "pdf_page": first["page"],
            "header_quote": first["line"],
            "detail_quote": detail_quote,
        }],
    }
    if reserve:
        result.update({
            "reserve_no": values["reserve_no"],
            "status": "standby_not_in_final_field",
            "scratched": False,
        })
    else:
        result.update({
            "horse_no": int(values["no"]),
            "declaration_flags": values.get("flags", "").split(),
            "minimum_weight_applied": bool(values.get("minimum_weight")),
            "probable_overweight_lb": int(values["probable_overweight_lb"]) if values.get("probable_overweight_lb") else None,
            "win_odds": None,
            "place_odds": None,
            "features": {},
            "intent_evidence": [],
        })
    return result


def parse_races(pages: list[dict], race_count: int) -> list[dict]:
    starts = find_race_starts(pages, race_count)
    races = []
    for start_index, (race_no, page_index) in enumerate(starts):
        end_page = starts[start_index + 1][1] if start_index + 1 < len(starts) else len(pages)
        entries = []
        for index in range(page_index, end_page):
            entries.extend({"page": pages[index]["page"], "line": line.strip()} for line in pages[index]["text"].splitlines())
        lines = [entry["line"] for entry in entries]
        compact_index = next((index for index, line in enumerate(lines) if re.match(rf"^{race_no}\s+下午", line)), None)
        if compact_index is None:
            exact_index = next(index for index, line in enumerate(lines) if line == str(race_no))
            chinese_header = lines[exact_index + 1]
            time_index, distance_index = exact_index + 2, exact_index + 3
        else:
            exact_index = compact_index
            chinese_header = lines[exact_index].split(maxsplit=1)[1]
            time_index, distance_index = exact_index + 1, exact_index + 2
        time_match = re.match(r"^(\d{1,2})\.(\d{2}) ([AP])\.M\.", lines[time_index])
        hour = int(time_match.group(1)) % 12 + (12 if time_match.group(3) == "P" else 0)
        off_time = f"{hour:02d}:{time_match.group(2)}"
        distance_match = re.search(r"(\d+)\s*米\s*METRES", lines[distance_index])
        if not distance_match:
            raise ValueError(f"Race {race_no}: no distance")
        race_name = re.sub(r"^下午\S+\s+", "", chinese_header).strip()
        race_name = re.split(r"\s+[A-Z]{2,}", race_name)[0].strip()
        class_name = None
        for line in lines[exact_index:exact_index + 10]:
            match = re.search(r"(第[一二三四五]班(?:（條件限制）)?|[一二三]級賽)", line)
            if match:
                class_name = match.group(1)
                break
        if not class_name:
            raise ValueError(f"Race {race_no}: no class")
        starts_found = []
        for index, entry in enumerate(entries):
            if RUNNER_RE.match(entry["line"]):
                starts_found.append((index, False))
            elif RESERVE_RE.match(entry["line"]):
                starts_found.append((index, True))
        runners, reserves = [], []
        for block_index, (line_index, reserve) in enumerate(starts_found):
            end = starts_found[block_index + 1][0] if block_index + 1 < len(starts_found) else len(entries)
            block = entries[line_index:end]
            (reserves if reserve else runners).append(parse_runner_block(block, reserve))
        active_counts = [int(match.group(1)) for line in lines if (match := re.search(r"(\d+) 匹出賽馬匹。", line))]
        if len(active_counts) != 1:
            raise ValueError(f"Race {race_no}: active count declarations {active_counts}")
        active = sum(not runner["scratched"] for runner in runners)
        if active != active_counts[0]:
            raise ValueError(f"Race {race_no}: PDF says {active_counts[0]} active, parsed {active}")
        numbers = [runner["horse_no"] for runner in runners]
        if len(numbers) != len(set(numbers)):
            raise ValueError(f"Race {race_no}: duplicate horse number")
        races.append({
            "race_no": race_no,
            "off_hhmm": off_time,
            "race_name": race_name,
            "distance_m": int(distance_match.group(1)),
            "class_name": class_name,
            "race_class": CLASS_NUMBERS.get(class_name[1]) if class_name.startswith("第") else None,
            "active_count": active,
            "runners": runners,
            "reserve_runners": reserves,
            "header_quote": "\n".join(lines[exact_index - 1:exact_index + 4]),
        })
    return races


def artifact(path: Path, root: Path, **extra) -> dict:
    return {"path": path.relative_to(root).as_posix(), "sha256": file_hash(path), **extra}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-HK,zh;q=0.9"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def apply_market(card: dict, market: dict, live_card: dict) -> None:
    race = card["race"]
    if market.get("race_id") != race["race_id"] or market.get("off_time") != race["off_time"]:
        raise ValueError(f"Market identity/off time mismatch for {race['race_id']}")
    if live_card.get("collection_mode") != "live" or live_card.get("data_cutoff") != market.get("page_updated_at"):
        raise ValueError("The R2 market record and original live card disagree")
    market_rows = {row["horse_no"]: row for row in market["runners"]}
    live_rows = {row["horse_no"]: row for row in live_card["runners"]}
    active = [runner for runner in card["runners"] if not runner["scratched"]]
    if set(market_rows) != {runner["horse_no"] for runner in active} or set(live_rows) != set(market_rows):
        raise ValueError("Market runner coverage mismatch")
    for runner in active:
        no = runner["horse_no"]
        for field in ["horse_name", "horse_id", "jockey", "trainer", "draw", "weight_lb", "rating"]:
            if runner[field] != live_rows[no].get(field):
                raise ValueError(f"PDF/live card mismatch R{race['race_no']} #{no} {field}: {runner[field]!r} != {live_rows[no].get(field)!r}")
        for field in ["win_odds", "place_odds"]:
            if market_rows[no].get(field) != live_rows[no].get(field):
                raise ValueError(f"Market/live card mismatch #{no} {field}")
            runner[field] = market_rows[no].get(field)
    card["data_cutoff"] = market["page_updated_at"]
    card["notes"]["market"] = " contemporaneous live page transcription; WIN/PLA only; odds are reference data, not intent evidence"


def result_from_html(html: bytes, card: dict, url: str, retrieved_at: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div.performance table")
    if table is None:
        raise ValueError(f"No official performance table: {url}")
    info_tables = [item for item in soup.find_all("table") if "場地狀況" in item.get_text() and "賽道" in item.get_text()]
    if len(info_tables) != 1:
        raise ValueError(f"Race information table count is {len(info_tables)}")
    compact_info = re.sub(r"\s+", "", info_tables[0].get_text(" ", strip=True))
    for expected in [card["race"]["race_name"], f"{card['race']['distance_m']}米", card["race"]["class_name"]]:
        if re.sub(r"\s+", "", expected) not in compact_info:
            raise ValueError(f"Official result metadata mismatch: {expected}")
    dividends = {}
    exotic_dividends = {
        pool: {"status": "final", "combinations": [], "refunds": []}
        for pool in ("Q", "PQ")
    }
    current_pool = None
    dividend_table = soup.select_one("div.dividend_tab table")
    if dividend_table:
        for row in dividend_table.select("tbody tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
            if not cells:
                continue
            if cells[0] and not re.fullmatch(r"[\d,\s]+", cells[0]):
                current_pool = cells[0]
                cells = cells[1:]
            if current_pool == "獨贏" and len(cells) >= 2:
                payout = number(cells[1])
                for token in cells[0].split(","):
                    if token.strip().isdigit() and payout is not None:
                        dividends[int(token.strip())] = payout
            elif current_pool in {"連贏", "位置Q"} and len(cells) >= 2:
                payout = number(cells[1])
                if payout is None:
                    raise ValueError(f"Missing official {current_pool} dividend: {cells}")
                pool = "Q" if current_pool == "連贏" else "PQ"
                exotic_dividends[pool]["combinations"].append({
                    "horses": parse_pair_text(cells[0]),
                    "dividend_hkd": payout,
                })
    rows = []
    for tr in table.select("tbody tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all("td", recursive=False)]
        if len(cells) != 12:
            raise ValueError(f"Unexpected result row with {len(cells)} cells: {cells}")
        label, no_text, name_id, jockey, trainer = cells[:5]
        name_match = re.fullmatch(r"(.*?)\s*\(([A-Z]\d+)\)", name_id)
        if not name_match or not no_text.isdigit():
            raise ValueError(f"Cannot parse result identity: {cells[:3]}")
        position_match = re.match(r"^(\d+)", label)
        position = int(position_match.group(1)) if position_match else None
        status = "OK" if label.isdigit() else label.replace(" ", "")
        no = int(no_text)
        rows.append({
            "horse_no": no,
            "horse_name": name_match.group(1),
            "horse_id": horse_id(name_match.group(2)),
            "jockey": jockey,
            "trainer": trainer,
            "finish_position": position,
            "status": status,
            "official_status": label,
            "actual_weight_lb": integer(cells[5]),
            "body_weight_lb": integer(cells[6]),
            "draw": integer(cells[7]),
            "margin": cells[8],
            "running_positions": cells[9],
            "finish_time": cells[10],
            "final_win_odds_reference": number(cells[11]),
            "win_dividend": dividends.get(no) if position == 1 else None,
        })
    return {
        "schema_version": "1.0",
        "race_id": card["race"]["race_id"],
        "recorded_at": retrieved_at,
        "race": {
            "race_name": card["race"]["race_name"],
            "distance_m": card["race"]["distance_m"],
            "class_name": card["race"]["class_name"],
            "official_info_verified": True,
        },
        "market_reference": {
            "kind": "historical_closing_reference",
            "pre_race_snapshot": False,
            "note": "Final WIN odds read from the post-race official result page; never injected into the historical pre-race card.",
        },
        "sources": [],
        "results": rows,
        "exotic_dividends": exotic_dividends,
    }


def validate_result_coverage(result: dict, card: dict) -> None:
    card_rows = {runner["horse_no"]: runner for runner in card["runners"]}
    result_rows = {row["horse_no"]: row for row in result["results"]}
    if set(result_rows) != set(card_rows):
        raise ValueError(f"Result coverage mismatch: missing={set(card_rows)-set(result_rows)}, extra={set(result_rows)-set(card_rows)}")
    validate_result(result, card)
    for no, runner in card_rows.items():
        row = result_rows[no]
        if row.get("horse_name") != runner["horse_name"] or row.get("horse_id") != runner["horse_id"]:
            raise ValueError(f"Result identity mismatch for horse {no}")
        if runner["scratched"]:
            if row["finish_position"] is not None or row["status"] not in SCRATCH_CODES:
                raise ValueError(f"Scratched horse {no} has an invalid result")
        elif row["status"] in SCRATCH_CODES or (row["finish_position"] is None and row["status"] not in NON_FINISH_CODES):
            raise ValueError(f"Active horse {no} lacks a finishing position or legal non-finish status")


def negative_controls(card: dict, result: dict) -> list[dict]:
    checks = []

    def expect_rejection(name, payload, validator):
        try:
            validator(payload)
        except ValueError:
            checks.append({"check": name, "passed": True})
        else:
            checks.append({"check": name, "passed": False})

    changed = copy.deepcopy(card)
    changed["data_cutoff"] = "2099-01-01T00:00:00+08:00"
    expect_rejection("post_off_cutoff_rejected", changed, validate_card)
    changed = copy.deepcopy(card)
    changed["sources"][0]["available_at"] = "2099-01-01T00:00:00+08:00"
    expect_rejection("post_cutoff_availability_rejected", changed, validate_card)
    changed = copy.deepcopy(card)
    changed["sources"][0].pop("availability_evidence")
    expect_rejection("missing_availability_proof_rejected", changed, validate_card)
    changed = copy.deepcopy(card)
    next(runner for runner in changed["runners"] if not runner["scratched"])["features"]["finish_position"] = 1
    expect_rejection("post_race_feature_rejected", changed, validate_card)
    changed = copy.deepcopy(result)
    changed["results"] = changed["results"][:-1]
    expect_rejection("missing_result_runner_rejected", changed, lambda item: validate_result_coverage(item, card))
    if any(runner["scratched"] for runner in card["runners"]):
        changed = copy.deepcopy(result)
        scratched = next(runner["horse_no"] for runner in card["runners"] if runner["scratched"])
        next(row for row in changed["results"] if row["horse_no"] == scratched)["finish_position"] = 1
        expect_rejection("scratched_finish_rejected", changed, lambda item: validate_result_coverage(item, card))
    changed = copy.deepcopy(result)
    active_positions = [row for row in changed["results"] if row["finish_position"] is not None]
    active_positions[1]["finish_position"] = 1
    try:
        validate_result_coverage(changed, card)
    except ValueError:
        checks.append({"check": "dead_heat_accepted", "passed": False})
    else:
        checks.append({"check": "dead_heat_accepted", "passed": True})
    return checks


def build(spec_path: Path, workspace: Path) -> dict:
    spec_path = spec_path.resolve()
    spec = read_json(spec_path)
    root = spec_path.parent.resolve()
    workspace = workspace.resolve()
    meeting = spec["meeting"]
    date, venue, race_count = meeting["date"], meeting["venue"], meeting["race_count"]
    source_records = [artifact(spec_path, root, kind="build_spec")]
    pdf_input = workspace / spec["racecard"]["path"]
    pdf_out = root / "sources" / "racecard" / pdf_input.name
    pdf_out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pdf_input, pdf_out)
    if file_hash(pdf_input) != file_hash(pdf_out):
        raise ValueError("PDF copy hash mismatch")
    pages = extract_pdf_pages(pdf_out)
    extraction = {"source_pdf_sha256": file_hash(pdf_out), "extractor": "pdfplumber.extract_text", "pages": pages}
    extraction_path = root / "sources" / "racecard" / "pages.json"
    write_json(extraction_path, extraction)
    source_records.extend([
        artifact(pdf_out, root, kind="racecard_pdf", retrieved_at=spec["racecard"]["retrieved_at"]),
        artifact(extraction_path, root, kind="racecard_text_derivative"),
    ])
    availability_path = workspace / spec["racecard"]["availability_evidence"]["path"]
    availability = read_json(availability_path)
    if availability["content_sha256"] != file_hash(pdf_out):
        raise ValueError("Attachment evidence does not match the PDF")
    if availability["available_at"] != spec["racecard"]["available_at"]:
        raise ValueError("Attachment availability time mismatch")
    source_records.append(artifact(availability_path, root, kind="availability_evidence"))
    if spec["racecard"]["content_as_of_quote"] not in pages[0]["text"]:
        raise ValueError("PDF content-as-of quote is missing")
    parsed_races = parse_races(pages, race_count)
    generated_at = now_hk()
    market_by_race = {item["race_no"]: item for item in spec.get("live_market", [])}
    cards, results = [], []
    for parsed in parsed_races:
        race_no = parsed["race_no"]
        race_id = f"{date}-{venue}-R{race_no}"
        cutoff = spec["racecard"]["data_cutoff"]
        pdf_source = {
            "source_id": "PDF",
            "title": "HKJC 出賽馬匹資料及往績（用戶提供原件）",
            "url": pdf_out.resolve().as_uri(),
            "local_path": pdf_out.relative_to(root).as_posix(),
            "retrieved_at": spec["racecard"]["retrieved_at"],
            "available_at": spec["racecard"]["available_at"],
            "content_as_of": spec["racecard"]["content_as_of"],
            "sha256": file_hash(pdf_out),
            "official": True,
            "official_download_verified": False,
            "availability_evidence": {
                "kind": "session_attachment",
                "reference": availability_path.relative_to(root).as_posix(),
                "sha256": file_hash(availability_path),
            },
            "excerpt": spec["racecard"]["content_as_of_quote"] + "\n" + parsed["header_quote"],
        }
        card = {
            "schema_version": "1.1",
            "collection_mode": "historical",
            "captured_at": generated_at,
            "data_cutoff": cutoff,
            "race": {
                "race_id": race_id,
                "meeting_date": date,
                "venue": venue,
                "race_no": race_no,
                "race_name": parsed["race_name"],
                "off_time": f"{date}T{parsed['off_hhmm']}:00+08:00",
                "distance_m": parsed["distance_m"],
                "declared_starters": parsed["active_count"],
                "place_slots": 3,
                "race_class": parsed["race_class"],
                "class_name": parsed["class_name"],
                "surface": "turf",
                "course": "A",
                "going": None,
            },
            "sources": [pdf_source],
            "runners": parsed["runners"],
            "reserve_runners": parsed["reserve_runners"],
            "notes": {
                "historical_reconstruction": True,
                "forward_kpi_eligible": False,
                "market": "no contemporaneous full-field market snapshot",
                "body_weight": "not available in the pre-race PDF; post-race body weights were not copied into this card",
                "intent": "no verified pre-race direct connection interviews collected; intent_evidence remains empty",
            },
        }
        if race_no in market_by_race:
            config = market_by_race[race_no]
            market_input = workspace / config["path"]
            live_input = workspace / config["live_card_path"]
            market = read_json(market_input)
            live_card = read_json(live_input)
            apply_market(card, market, live_card)
            market_out = root / "sources" / "market" / market_input.name
            live_out = root / "sources" / "market" / live_input.name
            market_out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(market_input, market_out)
            shutil.copyfile(live_input, live_out)
            source_records.extend([
                artifact(market_out, root, kind="live_market_transcription", retrieved_at=market["captured_at"]),
                artifact(live_out, root, kind="original_live_card", retrieved_at=live_card["captured_at"]),
            ])
            card["sources"].append({
                "source_id": "MARKET",
                "title": "HKJC 第2場 12:58 獨贏／位置頁面同時點轉錄",
                "url": market["url"],
                "local_path": market_out.relative_to(root).as_posix(),
                "retrieved_at": market["captured_at"],
                "available_at": market["page_updated_at"],
                "sha256": file_hash(market_out),
                "official": True,
                "capture_method": "manual_transcription_from_live_page",
                "availability_evidence": {
                    "kind": "contemporaneous_snapshot",
                    "reference": market_out.relative_to(root).as_posix(),
                    "sha256": file_hash(market_out),
                },
                "excerpt": next(source["excerpt"] for source in live_card["sources"] if source["source_id"] == "S1"),
            })
        card_path = root / "cards" / f"{race_id}.json"
        validate_card(card)
        write_json(card_path, card)
        cards.append(card)
        url = spec["results"]["url_template"].format(race_no=race_no)
        result_html_path = root / "sources" / "results" / f"R{race_no}.html"
        sidecar_path = root / "sources" / "results" / f"R{race_no}.source.json"
        if spec["results"].get("fetch"):
            html = download(url)
            retrieved_at = now_hk()
            result_html_path.parent.mkdir(parents=True, exist_ok=True)
            result_html_path.write_bytes(html)
            write_json(sidecar_path, {"url": url, "retrieved_at": retrieved_at, "sha256": file_hash(result_html_path)})
        else:
            if not result_html_path.exists() or not sidecar_path.exists():
                raise ValueError(f"Offline result source missing for R{race_no}")
            html = result_html_path.read_bytes()
            sidecar = read_json(sidecar_path)
            retrieved_at = sidecar["retrieved_at"]
            if sidecar["sha256"] != file_hash(result_html_path):
                raise ValueError(f"Cached result hash mismatch for R{race_no}")
        result = result_from_html(html, card, url, retrieved_at)
        result["sources"] = [{
            "source_id": "RESULT",
            "title": f"HKJC 官方第{race_no}場賽果",
            "url": url,
            "local_path": result_html_path.relative_to(root).as_posix(),
            "retrieved_at": retrieved_at,
            "published_at": None,
            "publication_time_note": "The exact first-publication timestamp is not exposed; retrieved_at is the actual download time.",
            "sha256": file_hash(result_html_path),
            "official": True,
        }]
        validate_result_coverage(result, card)
        result_path = root / "results" / f"{race_id}.json"
        write_json(result_path, result)
        results.append(result)
        source_records.extend([
            artifact(result_html_path, root, kind="official_result_html", retrieved_at=retrieved_at, url=url),
            artifact(sidecar_path, root, kind="official_result_source_record"),
        ])
    for probe in spec.get("odds_probes", []):
        probe_path = workspace / probe
        target = root / "sources" / "odds-probe" / probe_path.name
        if probe_path.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(probe_path, target)
        source_records.append(artifact(target, root, kind="failed_odds_api_probe"))
    database = root / spec["database"]
    init_db(str(database))
    import_ids = {"cards": [], "results": []}
    for card, result in zip(cards, results):
        card_path = root / "cards" / f"{card['race']['race_id']}.json"
        result_path = root / "results" / f"{result['race_id']}.json"
        card_args = argparse.Namespace(file=str(card_path), db=str(database), allow_unknown_features=False)
        result_args = argparse.Namespace(file=str(result_path), db=str(database))
        import_ids["cards"].append(command_import_card(card_args))
        import_ids["results"].append(command_import_result(result_args))
    live_races = [card["race"]["race_no"] for card in cards if all(
        runner["win_odds"] is not None for runner in card["runners"] if not runner["scratched"]
    )]
    manifest = {
        "schema_version": "p0-meeting/1.0",
        "meeting": meeting,
        "generated_at": generated_at,
        "collection_mode": "historical",
        "forward_kpi_eligible": False,
        "database": spec["database"],
        "sources": source_records,
        "cards": [artifact(root / "cards" / f"{card['race']['race_id']}.json", root, race_no=card["race"]["race_no"]) for card in cards],
        "results": [artifact(root / "results" / f"{result['race_id']}.json", root, race_no=int(result["race_id"].rsplit("R", 1)[1])) for result in results],
        "coverage": {
            "races": len(cards),
            "live_pre_race_market_races": live_races,
            "historical_closing_reference_races": list(range(1, race_count + 1)),
            "historical_closing_reference_is_pre_race": False,
        },
        "limitations": [
            "Only R2 has a contemporaneous full-field WIN/PLA page transcription; the other ten races lack a verifiable pre-race odds snapshot.",
            "The R2 odds record is a live page transcription, not a raw replayable odds API response.",
            "Final WIN odds on result pages are post-race historical closing references and are excluded from every pre-race card.",
            "No prediction, model promotion or forward KPI is produced by this engineering chain.",
            "The PDF attachment timestamp is session evidence, not independent third-party notarisation.",
        ],
        "imports": import_ids,
    }
    write_json(root / "meeting-manifest.json", manifest)
    report = validate_meeting(root)
    write_json(root / "validation-report.json", report)
    if report["errors"]:
        raise ValueError(f"Meeting validation failed: {report['errors']}")
    return report


def validate_meeting(root: Path) -> dict:
    root = root.resolve()
    manifest = read_json(root / "meeting-manifest.json")
    errors, warnings = [], []
    for item in manifest["sources"] + manifest["cards"] + manifest["results"]:
        path = root / item["path"]
        if not path.exists() or file_hash(path) != item["sha256"]:
            errors.append(f"Hash/missing artifact: {item['path']}")
    cards = [read_json(root / item["path"]) for item in manifest["cards"]]
    results = [read_json(root / item["path"]) for item in manifest["results"]]
    expected_numbers = list(range(1, manifest["meeting"]["race_count"] + 1))
    if [card["race"]["race_no"] for card in cards] != expected_numbers:
        errors.append("Race sequence/coverage mismatch")
    extraction = read_json(root / "sources" / "racecard" / "pages.json")
    pages = {page["page"]: page["text"] for page in extraction["pages"]}
    race_summaries = []
    controls = []
    for card, result in zip(cards, results):
        race_no = card["race"]["race_no"]
        try:
            validate_card(card)
            if card["collection_mode"] != "historical" or card.get("notes", {}).get("forward_kpi_eligible") is not False:
                raise ValueError("P0 cards must be historical and excluded from forward KPI")
            validate_result_coverage(result, card)
        except ValueError as exc:
            errors.append(f"R{race_no}: {exc}")
            continue
        active = [runner for runner in card["runners"] if not runner["scratched"]]
        scratched = [runner for runner in card["runners"] if runner["scratched"]]
        required = ["horse_no", "horse_name", "horse_id", "jockey", "trainer", "draw", "weight_lb", "rating"]
        missing_core = [f"#{runner['horse_no']}:{field}" for runner in active for field in required if runner.get(field) is None]
        if missing_core:
            errors.append(f"R{race_no} active core fields missing: {missing_core}")
        if card["race"]["declared_starters"] != len(active):
            errors.append(f"R{race_no} declared/active mismatch")
        source_ids = {source["source_id"] for source in card["sources"]}
        for runner in card["runners"]:
            for ref in runner.get("source_refs", []):
                if ref["source_id"] not in source_ids or ref["header_quote"] not in pages.get(ref["pdf_page"], "") or ref["detail_quote"] not in pages.get(ref["pdf_page"], ""):
                    errors.append(f"R{race_no} #{runner['horse_no']} source quote/page mismatch")
        reserves = card.get("reserve_runners", [])
        reserve_ids = [item["reserve_no"] for item in reserves]
        if len(reserve_ids) != len(set(reserve_ids)) or any(not re.fullmatch(r"S\d+", item) for item in reserve_ids):
            errors.append(f"R{race_no} invalid reserve identifiers")
        live_market = all(runner["win_odds"] is not None and runner["place_odds"] is not None for runner in active)
        closing_references = sum(row.get("final_win_odds_reference") is not None for row in result["results"])
        race_summaries.append({
            "race_no": race_no,
            "active": len(active),
            "scratched": len(scratched),
            "scratched_horse_nos": [runner["horse_no"] for runner in scratched],
            "reserves": reserve_ids,
            "result_rows": len(result["results"]),
            "live_pre_race_market": live_market,
            "closing_odds_reference_rows": closing_references,
            "core_fields_complete": not missing_core,
        })
        if race_no == 1:
            controls = negative_controls(card, result)
    failed_controls = [item["check"] for item in controls if not item["passed"]]
    errors.extend(f"Negative control failed: {name}" for name in failed_controls)
    database = root / manifest["database"]
    with connect(str(database)) as db:
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in [
            "races", "card_snapshots", "result_snapshots", "prediction_runs", "models", "evaluation_runs"
        ]}
        modes = [row[0] for row in db.execute("SELECT json_extract(payload_json, '$.collection_mode') FROM card_snapshots")]
    if counts["races"] != len(cards) or counts["card_snapshots"] < len(cards) or counts["result_snapshots"] < len(results):
        errors.append(f"Database coverage mismatch: {counts}")
    if any(mode != "historical" for mode in modes):
        errors.append("Live/historical database separation failed")
    if counts["prediction_runs"] or counts["models"] or counts["evaluation_runs"]:
        errors.append("P0 engineering database unexpectedly contains predictions/models/evaluations")
    total_active = sum(item["active"] for item in race_summaries)
    total_scratched = sum(item["scratched"] for item in race_summaries)
    live_market_races = [item["race_no"] for item in race_summaries if item["live_pre_race_market"]]
    report = {
        "schema_version": "p0-validation/1.0",
        "validated_at": now_hk(),
        "meeting": manifest["meeting"],
        "engineering_status": "passed" if not errors else "failed",
        "p0_acceptance_status": "complete" if not errors and len(live_market_races) == len(cards) else "incomplete_market_history",
        "forward_kpi_eligible": False,
        "errors": errors,
        "warnings": warnings,
        "totals": {
            "races": len(race_summaries),
            "active_runners": total_active,
            "scratched_runners": total_scratched,
            "reserve_runners": sum(len(item["reserves"]) for item in race_summaries),
            "result_rows": sum(item["result_rows"] for item in race_summaries),
            "live_pre_race_market_races": len(live_market_races),
            "live_pre_race_market_race_nos": live_market_races,
            "pre_race_market_race_coverage": len(live_market_races) / len(cards) if cards else 0,
            "active_core_field_coverage": 1.0 if all(item["core_fields_complete"] for item in race_summaries) else 0.0,
        },
        "races": race_summaries,
        "negative_controls": controls,
        "database_counts": counts,
        "database_collection_modes": sorted(set(modes)),
        "limitations": manifest["limitations"],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--spec", required=True)
    build_parser.add_argument("--workspace", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--meeting-dir", required=True)
    args = parser.parse_args()
    if args.command == "build":
        report = build(Path(args.spec), Path(args.workspace))
    else:
        report = validate_meeting(Path(args.meeting_dir))
        write_json(Path(args.meeting_dir) / "validation-report.json", report)
    print(json.dumps({
        "engineering_status": report["engineering_status"],
        "p0_acceptance_status": report["p0_acceptance_status"],
        "totals": report["totals"],
        "errors": report["errors"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["engineering_status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
