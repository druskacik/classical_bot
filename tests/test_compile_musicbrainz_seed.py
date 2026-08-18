import json
import unittest

from source_discovery.musicbrainz.compile_seed import compile_rows, same_site


def review(**overrides: str) -> dict[str, str]:
    row = {
        "candidate_id": "MBURL0001",
        "decision": "include",
        "calendar_status": "current_events",
        "final_url": "https://artist.example/",
        "review_url": "https://artist.example/",
        "event_page_url": "https://artist.example/concerts",
        "entity_types_json": json.dumps(["Person"]),
        "countries_json": json.dumps(["CZ"]),
        "musicbrainz_ids_json": json.dumps(["mbid-1"]),
        "entity_names_json": json.dumps(["Example Artist"]),
    }
    row.update(overrides)
    return row


class CompileMusicBrainzSeedTests(unittest.TestCase):
    def test_same_site_accepts_subdomains_but_not_third_parties(self) -> None:
        self.assertTrue(
            same_site("https://artist.example/", "https://tour.artist.example/")
        )
        self.assertFalse(
            same_site("https://artist.example/", "https://tickets.example/")
        )

    def test_compile_includes_people_and_omits_inferred_country(self) -> None:
        seeds, counts = compile_rows([review()], set())

        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["url"], "https://artist.example/")
        self.assertEqual(seeds[0]["country_code"], "")
        self.assertEqual(seeds[0]["crawler_path"], "")
        self.assertEqual(counts["included_hosts"], 1)

    def test_compile_filters_noncurrent_cross_host_and_known_hosts(self) -> None:
        reviews = [
            review(candidate_id="MBURL0001", calendar_status="past_events_only"),
            review(
                candidate_id="MBURL0002",
                final_url="https://cross-host.example/",
                review_url="https://cross-host.example/",
                event_page_url="https://tickets.example/event",
            ),
            review(
                candidate_id="MBURL0003",
                final_url="https://known.example/",
                review_url="https://known.example/",
                event_page_url="https://known.example/events",
            ),
        ]

        seeds, counts = compile_rows(reviews, {"known.example"})

        self.assertEqual(seeds, [])
        self.assertEqual(counts["past_events_only"], 1)
        self.assertEqual(counts["cross_host_evidence"], 1)
        self.assertEqual(counts["existing_seed_host"], 1)

    def test_compile_consolidates_shared_host_and_preserves_audit_ids(self) -> None:
        rows = [
            review(),
            review(
                candidate_id="MBURL0002",
                final_url="https://www.artist.example/about",
                review_url="https://www.artist.example/about",
                musicbrainz_ids_json=json.dumps(["mbid-2"]),
                entity_names_json=json.dumps(["Second Artist"]),
            ),
        ]

        seeds, counts = compile_rows(rows, set())

        self.assertEqual(len(seeds), 1)
        self.assertEqual(counts["included_entities"], 2)
        self.assertEqual(counts["consolidated_same_host"], 1)
        self.assertIn("MBURL0001,MBURL0002", seeds[0]["notes"])
        self.assertIn("mbid-1,mbid-2", seeds[0]["notes"])


if __name__ == "__main__":
    unittest.main()
