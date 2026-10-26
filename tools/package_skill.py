#!/usr/bin/env python3
"""Validate and package predict-hk-racing without third-party dependencies."""
from __future__ import annotations

import argparse
import hashlib
import sys
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "predict-hk-racing"
REQUIRED = [
    "SKILL.md",
    "references/data-collection.md",
    "references/intent-model.md",
    "references/modeling-and-backtesting.md",
    "references/schemas-and-cli.md",
    "assets/race-card-template.json",
    "assets/race-result-template.json",
    "scripts/racing_model.py",
    "scripts/racing_lab.py",
]


def validate() -> list[Path]:
    if not SKILL.is_dir():
        raise ValueError(f"Missing skill directory: {SKILL}")
    for relative in REQUIRED:
        if not (SKILL / relative).is_file():
            raise ValueError(f"Missing required file: {relative}")

    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) != 3:
        raise ValueError("Unclosed YAML frontmatter")
    lines = parts[1].strip().splitlines()
    keys = [match.group(1) for line in lines if (match := re.match(r"^([A-Za-z0-9_-]+):", line))]
    if keys != ["name", "description"]:
        raise ValueError(f"Frontmatter must contain only name and description: {keys}")
    name = next(line.split(":", 1)[1].strip() for line in lines if line.startswith("name:"))
    if name != SKILL.name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ValueError("Skill name must match its folder and use lowercase hyphen-case")
    description = " ".join(line.strip() for line in lines if line.startswith(" ") or line.startswith("description:"))
    for phrase in ["香港賽馬", "去馬", "回測", "修正"]:
        if phrase not in description:
            raise ValueError(f"Description is missing trigger/context: {phrase}")
    if len(parts[2].splitlines()) >= 500:
        raise ValueError("SKILL.md body must stay below 500 lines")
    for target in re.findall(r"\]\((references/[^)]+\.md)\)", parts[2]):
        if not (SKILL / target).is_file():
            raise ValueError(f"Broken reference link: {target}")

    for path in (SKILL / "scripts").glob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    for path in (SKILL / "assets").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))

    files = sorted(
        path for path in SKILL.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if any(path.name.lower() in {"readme.md", "changelog.md", "installation_guide.md"} for path in files):
        raise ValueError("Repo-level documentation must not be placed inside the Skill package")
    return files


def package(files: list[Path], output: Path) -> tuple[Path, str]:
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "predict-hk-racing.skill"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(archive, "r") as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        if f"{SKILL.name}/SKILL.md" not in bundle.namelist():
            raise ValueError("Archive does not contain the expected SKILL.md")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS.txt").write_text(f"{digest}  predict-hk-racing.skill\n", encoding="utf-8")
    return archive, digest


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Package directory; omit with --check-only")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        files = validate()
        result = {"status": "PASS", "skill": SKILL.name, "files": len(files)}
        if not args.check_only:
            archive, digest = package(files, args.output or ROOT / "dist")
            result.update({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": digest})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
