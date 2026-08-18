from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from source_discovery.musicbrainz.merge_official_url_reviews import (
    read_rows,
    validate_review,
)
from source_discovery.musicbrainz.prepare_official_url_review import (
    DECISION_FIELDS,
    EVIDENCE_FIELDS,
    FIELDNAMES,
)


DEFAULT_INPUT = Path("data/musicbrainz_official_url_review.csv")
DEFAULT_SHARD_DIR = Path("data/musicbrainz_official_url_focused_review_shards")
DEFAULT_OUTPUT = Path("data/musicbrainz_official_url_review_v2.csv")
HISTORY_FIELDS = [f"first_pass_{field}" for field in DECISION_FIELDS]
FOCUSED_FIELDNAMES = FIELDNAMES + HISTORY_FIELDS


def history_values(row: dict[str, str]) -> dict[str, str]:
    return {f"first_pass_{field}": row[field] for field in DECISION_FIELDS}


def write_focused(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FOCUSED_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def prepare(input_path: Path, shard_dir: Path, shards: int) -> int:
    if shards <= 0:
        raise ValueError("shards must be positive")
    rows = read_rows(input_path)
    targets = []
    for row in rows:
        if row["decision"] != "needs_deeper_review":
            continue
        targets.append(
            {
                **row,
                **dict.fromkeys(DECISION_FIELDS, ""),
                **history_values(row),
            }
        )
    for shard in range(shards):
        path = shard_dir / f"shard_{shard + 1:02d}.csv"
        write_focused(path, targets[shard::shards])
        print(f"Wrote {len(targets[shard::shards])} candidates to {path}")
    return len(targets)


def read_focused(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FOCUSED_FIELDNAMES:
            raise ValueError(f"{path} has unexpected columns")
        return list(reader)


def merge(input_path: Path, shard_dir: Path, output_path: Path) -> list[dict[str, str]]:
    base_rows = read_rows(input_path)
    base = {row["candidate_id"]: row for row in base_rows}
    expected = {row["candidate_id"] for row in base_rows if row["decision"] == "needs_deeper_review"}
    reviewed: dict[str, dict[str, str]] = {}
    for path in sorted(shard_dir.glob("shard_*.csv")):
        for row in read_focused(path):
            candidate_id = row["candidate_id"]
            if candidate_id not in expected:
                raise ValueError(f"{path}: unexpected focused candidate {candidate_id}")
            if candidate_id in reviewed:
                raise ValueError(f"{path}: duplicate focused candidate {candidate_id}")
            original = base[candidate_id]
            for field in EVIDENCE_FIELDS:
                if row[field] != original[field]:
                    raise ValueError(f"{path}: {candidate_id} changed evidence field {field}")
            for field in DECISION_FIELDS:
                if row[f"first_pass_{field}"] != original[field]:
                    raise ValueError(f"{path}: {candidate_id} changed first-pass field {field}")
            validate_review(row, path)
            reviewed[candidate_id] = row
    missing = sorted(expected - set(reviewed))
    if missing:
        raise ValueError(f"Missing {len(missing)} focused reviews; first: {', '.join(missing[:10])}")

    merged = []
    for original in base_rows:
        candidate_id = original["candidate_id"]
        merged.append(reviewed.get(candidate_id) or {**original, **history_values(original)})
    write_focused(output_path, merged)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or merge the focused MusicBrainz URL review.")
    parser.add_argument("mode", choices={"prepare", "merge"})
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--shard-dir", type=Path, default=DEFAULT_SHARD_DIR)
    parser.add_argument("--shards", type=int, default=3)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if args.mode == "prepare":
        count = prepare(args.input, args.shard_dir, args.shards)
        print(f"Prepared {count} focused-review candidates")
        return

    rows = merge(args.input, args.shard_dir, args.output)
    print(f"Wrote {len(rows)} version-2 audit rows to {args.output}")
    print("Decisions:", dict(sorted(Counter(row["decision"] for row in rows).items())))
    transitions = Counter(
        (row["first_pass_decision"], row["decision"])
        for row in rows
        if row["first_pass_decision"] != row["decision"]
    )
    print("Changed decisions:", dict(sorted(transitions.items())))


if __name__ == "__main__":
    main()
