from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from source_discovery.bachtrack.compile_seed import crawler_slug, normalize_url
from source_discovery.classicalconcertmap import SEED_FIELDS


DEFAULT_REVIEW = Path("data/musicbrainz_official_url_review_v2.csv")
DEFAULT_SEED_DIR = Path("seeds/crawler_sources")
DEFAULT_OUTPUT = DEFAULT_SEED_DIR / "0007_musicbrainz_discovered_sources.csv"
SUPPORTED_TYPES = {"Person", "Group", "Orchestra", "Choir"}
ORGANIZATION_TYPES = {"Group", "Orchestra", "Choir"}
TYPE_PRIORITY = {"Orchestra": 0, "Choir": 1, "Group": 2, "Person": 3}
MUSICBRAINZ_PSEUDO_COUNTRIES = {"XE", "XW"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def host(url: str) -> str:
    parsed = urlsplit(url if "://" in url else f"https://{url}")
    value = (parsed.hostname or "").lower().encode("idna").decode("ascii")
    return value[4:] if value.startswith("www.") else value


def same_site(left: str, right: str) -> bool:
    left_host = host(left)
    right_host = host(right)
    return bool(
        left_host
        and right_host
        and (
            left_host == right_host
            or left_host.endswith(f".{right_host}")
            or right_host.endswith(f".{left_host}")
        )
    )


def existing_hosts(seed_dir: Path, output: Path) -> set[str]:
    values = set()
    for path in sorted(seed_dir.glob("*.csv")):
        if path.resolve() == output.resolve():
            continue
        for row in read_csv(path):
            for field in ("url", "canonical_url"):
                if value := (row.get(field) or "").strip():
                    values.add(host(value))
    return values


def entity_types(row: dict[str, str]) -> set[str]:
    return {value for value in json.loads(row["entity_types_json"]) if value}


def source_url(row: dict[str, str]) -> str:
    return normalize_url(row["final_url"] or row["review_url"])


def representative_key(row: dict[str, str]) -> tuple[int, int, str]:
    types = entity_types(row)
    priority = min((TYPE_PRIORITY.get(value, 99) for value in types), default=99)
    path_length = len(urlsplit(source_url(row)).path or "/")
    return priority, path_length, row["candidate_id"]


def organization_country(rows: list[dict[str, str]], representative: dict[str, str]) -> str:
    if not (entity_types(representative) & ORGANIZATION_TYPES):
        return ""
    countries = {
        value
        for row in rows
        for value in json.loads(row["countries_json"])
        if len(value) == 2 and value.isalpha() and value not in MUSICBRAINZ_PSEUDO_COUNTRIES
    }
    return countries.pop() if len(countries) == 1 else ""


def seed_notes(rows: list[dict[str, str]]) -> str:
    candidate_ids = sorted({row["candidate_id"] for row in rows})
    mbids = sorted(
        {
            value
            for row in rows
            for value in json.loads(row["musicbrainz_ids_json"])
            if value
        }
    )
    entities = sorted(
        {
            name
            for row in rows
            for name in json.loads(row["entity_names_json"])
            if name
        }
    )
    types = sorted({value for row in rows for value in entity_types(row)})
    evidence = sorted({row["event_page_url"] for row in rows if row["event_page_url"]})
    return (
        "Discovered via MusicBrainz official-homepage review v2; "
        f"candidates={','.join(candidate_ids)}; "
        f"musicbrainz_ids={','.join(mbids)}; "
        f"entities={' | '.join(entities)}; entity_types={','.join(types)}; "
        "calendar_status=current_events; review_confidence=high; "
        f"evidence={' | '.join(evidence)}"
    )


def compile_rows(
    reviews: list[dict[str, str]], known_hosts: set[str]
) -> tuple[list[dict[str, str]], dict[str, int]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    counts = defaultdict(int)
    for row in reviews:
        if row["decision"] != "include":
            counts[row["decision"]] += 1
            continue
        if row["calendar_status"] != "current_events":
            counts["past_events_only"] += 1
            continue
        if not (entity_types(row) & SUPPORTED_TYPES):
            counts["unsupported_entity_type"] += 1
            continue
        if row["event_page_url"] and not same_site(source_url(row), row["event_page_url"]):
            counts["cross_host_evidence"] += 1
            continue
        source_host = host(source_url(row))
        if source_host in known_hosts:
            counts["existing_seed_host"] += 1
            continue
        grouped[source_host].append(row)

    seed_rows = []
    for source_host, rows in grouped.items():
        representative = min(rows, key=representative_key)
        url = source_url(representative)
        country = organization_country(rows, representative)
        crawler_path = (
            f"crawlers/{country.lower()}/{crawler_slug(url)}" if country else ""
        )
        seed_rows.append(
            {
                "url": url,
                "country_code": country,
                "scope_hint": "",
                "canonical_url": "",
                "crawler_path": crawler_path,
                "priority": "0",
                "notes": seed_notes(rows),
            }
        )
        counts["included_entities"] += len(rows)
        counts["included_hosts"] += 1
        counts["consolidated_same_host"] += len(rows) - 1

    seed_rows.sort(key=lambda row: (row["country_code"], row["url"]))
    return seed_rows, dict(counts)


def write_seed(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SEED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile reviewed MusicBrainz homepages into one crawler-source seed."
    )
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--seed-dir", type=Path, default=DEFAULT_SEED_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rows, counts = compile_rows(
        read_csv(args.review), existing_hosts(args.seed_dir, args.output)
    )
    write_seed(args.output, rows)
    print(f"Wrote {len(rows)} MusicBrainz sources to {args.output}")
    for label, count in sorted(counts.items()):
        print(f"{label}: {count}")


if __name__ == "__main__":
    main()
