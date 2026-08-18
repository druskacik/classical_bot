from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from source_discovery.musicbrainz.prepare_official_url_review import (
    DECISION_FIELDS,
    EVIDENCE_FIELDS,
    FIELDNAMES,
    write_rows,
)


DEFAULT_MANIFEST = Path("data/musicbrainz_official_url_review_manifest.csv")
DEFAULT_SHARD_DIR = Path("data/musicbrainz_official_url_review_shards")
DEFAULT_OUTPUT = Path("data/musicbrainz_official_url_review.csv")

ALLOWED = {
    "decision": {"include", "exclude", "needs_deeper_review", "unreachable"},
    "homepage_status": {
        "reachable",
        "access_blocked",
        "not_found",
        "timeout",
        "tls_error",
        "other_error",
    },
    "identity_match": {"yes", "partial", "no", "unknown"},
    "calendar_status": {
        "current_events",
        "past_events_only",
        "calendar_no_dates",
        "no_calendar",
        "unknown",
    },
    "geographic_scope": {"country", "multi_country", "unknown"},
    "confidence": {"high", "medium", "low"},
}
REQUIRED_REVIEW_FIELDS = {
    "decision",
    "homepage_status",
    "identity_match",
    "calendar_status",
    "geographic_scope",
    "confidence",
    "evidence",
    "reviewed_by",
    "reviewed_at",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != FIELDNAMES:
            raise ValueError(f"{path} has unexpected columns")
        return list(reader)


def validate_review(row: dict[str, str], path: Path) -> None:
    candidate_id = row["candidate_id"]
    missing = sorted(field for field in REQUIRED_REVIEW_FIELDS if not row[field].strip())
    if missing:
        raise ValueError(f"{path}: {candidate_id} lacks {', '.join(missing)}")
    for field, values in ALLOWED.items():
        if row[field] not in values:
            raise ValueError(f"{path}: {candidate_id} has invalid {field}={row[field]!r}")
    country = row["country_code"].strip()
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError(f"{path}: {candidate_id} has invalid country_code={country!r}")
    if row["geographic_scope"] == "multi_country" and country:
        raise ValueError(f"{path}: {candidate_id} is multi_country but has country_code")
    try:
        datetime.fromisoformat(row["reviewed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path}: {candidate_id} has invalid reviewed_at") from exc


def merge(manifest_path: Path, shard_dir: Path) -> list[dict[str, str]]:
    manifest = read_rows(manifest_path)
    expected = {row["candidate_id"]: row for row in manifest}
    reviewed: dict[str, dict[str, str]] = {}
    for path in sorted(shard_dir.glob("shard_*.csv")):
        for row in read_rows(path):
            candidate_id = row["candidate_id"]
            if candidate_id not in expected:
                raise ValueError(f"{path}: unexpected candidate {candidate_id}")
            if candidate_id in reviewed:
                raise ValueError(f"{path}: duplicate candidate {candidate_id}")
            for field in EVIDENCE_FIELDS:
                if row[field] != expected[candidate_id][field]:
                    raise ValueError(f"{path}: {candidate_id} changed evidence field {field}")
            validate_review(row, path)
            reviewed[candidate_id] = row
    missing = sorted(set(expected) - set(reviewed))
    if missing:
        raise ValueError(f"Missing {len(missing)} reviews; first: {', '.join(missing[:10])}")
    return [reviewed[row["candidate_id"]] for row in manifest]


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and merge MusicBrainz URL-review shards.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--shard-dir", type=Path, default=DEFAULT_SHARD_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows = merge(args.manifest, args.shard_dir)
    write_rows(args.output, rows)
    print(f"Wrote {len(rows)} audited decisions to {args.output}")
    for decision, count in sorted(Counter(row["decision"] for row in rows).items()):
        print(f"{decision}: {count}")


if __name__ == "__main__":
    main()
