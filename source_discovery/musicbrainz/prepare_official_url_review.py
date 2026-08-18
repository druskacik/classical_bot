from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


DEFAULT_INPUT = Path("data/musicbrainz_classical_artists_with_urls.csv")
DEFAULT_OUTPUT = Path("data/musicbrainz_official_url_review_manifest.csv")

EVIDENCE_FIELDS = [
    "candidate_id",
    "review_url",
    "normalized_url",
    "host",
    "alternate_urls_json",
    "musicbrainz_ids_json",
    "entity_names_json",
    "entity_types_json",
    "countries_json",
    "entity_ended_values_json",
    "classical_tag_scores_json",
]
DECISION_FIELDS = [
    "decision",
    "homepage_status",
    "final_url",
    "identity_match",
    "calendar_status",
    "event_page_url",
    "geographic_scope",
    "country_code",
    "confidence",
    "evidence",
    "reviewed_by",
    "reviewed_at",
]
FIELDNAMES = EVIDENCE_FIELDS + DECISION_FIELDS


def normalized_url(value: str) -> str:
    parsed = urlsplit(value.strip() if "://" in value else f"https://{value.strip()}")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Official homepage must be HTTP(S): {value!r}")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host if not port or port in {80, 443} else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def json_values(values: set[str]) -> str:
    return json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))


def prepare_rows(input_path: Path) -> list[dict[str, str]]:
    grouped: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    with input_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for homepage in (row.get("official_homepages") or "").split("|"):
                homepage = homepage.strip()
                if not homepage:
                    continue
                normalized = normalized_url(homepage)
                values = grouped[normalized]
                values["alternate_urls"].add(homepage)
                values["musicbrainz_ids"].add(row.get("musicbrainz_id") or "")
                values["entity_names"].add(row.get("name") or "")
                values["entity_types"].add(row.get("type") or "")
                values["countries"].add(row.get("country") or "")
                values["entity_ended_values"].add(row.get("ended") or "")
                values["classical_tag_scores"].add(row.get("classical_tag_score") or "")

    rows = []
    for index, normalized in enumerate(sorted(grouped), start=1):
        values = grouped[normalized]
        alternates = sorted(values["alternate_urls"], key=lambda url: (url.startswith("http:"), url))
        rows.append(
            {
                "candidate_id": f"MBURL{index:04d}",
                "review_url": alternates[0],
                "normalized_url": normalized,
                "host": urlsplit(normalized).hostname or "",
                "alternate_urls_json": json_values(values["alternate_urls"]),
                "musicbrainz_ids_json": json_values(values["musicbrainz_ids"]),
                "entity_names_json": json_values(values["entity_names"]),
                "entity_types_json": json_values(values["entity_types"]),
                "countries_json": json_values(values["countries"]),
                "entity_ended_values_json": json_values(values["entity_ended_values"]),
                "classical_tag_scores_json": json_values(values["classical_tag_scores"]),
                **dict.fromkeys(DECISION_FIELDS, ""),
            }
        )
    return rows


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an auditable MusicBrainz homepage review manifest.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--shard-dir", type=Path)
    parser.add_argument("--shards", type=int, default=0)
    args = parser.parse_args()
    if bool(args.shard_dir) != bool(args.shards):
        raise SystemExit("--shard-dir and a positive --shards value must be supplied together")
    if args.shards < 0:
        raise SystemExit("--shards must not be negative")

    rows = prepare_rows(args.input)
    write_rows(args.output, rows)
    print(f"Wrote {len(rows)} normalized official-homepage candidates to {args.output}")
    if args.shard_dir:
        for shard in range(args.shards):
            shard_rows = rows[shard::args.shards]
            path = args.shard_dir / f"shard_{shard + 1:02d}.csv"
            write_rows(path, shard_rows)
            print(f"Wrote {len(shard_rows)} candidates to {path}")


if __name__ == "__main__":
    main()
