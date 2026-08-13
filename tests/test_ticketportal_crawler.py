from datetime import date, datetime, timezone
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from crawlers.cz.ticketportal_cz.main import TicketportalCzCrawler
from crawlers.sk.ticketportal_sk.main import TicketportalSkCrawler
from crawlers.ticketportal import (
    TICKETPORTAL_EPOCH_MINUTES,
    decode_events,
    decode_occurrences,
    decode_outputs,
    decode_venues,
    decode_cities,
    is_auxiliary_title,
    is_live_film_music_event,
    ticketportal_datetime,
)


def encoded_minute(year, month, day, hour, minute=0):
    instant = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return int(instant.timestamp() // 60) - TICKETPORTAL_EPOCH_MINUTES


def grid_fixture():
    start = encoded_minute(2030, 7, 1, 18)
    arrays = {
        "data_kategorie": [
            1004, "Filmová hudba", 1000, "",
            1008, "Klasická", 1000, "",
            1102, "Balet", 1100, "",
            1115, "Opera", 1100, "",
            1917, "Projekce", 1900, "",
        ],
        "data_mesto": [30, "Praha", 1, None],
        "data_hladisko": [40, "Rudolfinum", 30, "Alšovo nábřeží", "", "", "", "", 0, 0],
        "data_podujatie_out": [50, "Abonentní večer", "abonentni-vecer", "", "", 1, "Komorní koncert", "komorni-koncert", "", "", 1, "Parkovací lístek - orchestr", "parking", "", ""],
        "data_podujatie": [
            10, "Abonentní večer", 1000, [1008], 0, "image", 50, False, 0,
            1, "Komorní koncert", 1000, None, 0, "image", 1, False, 0,
            1, "Parkovací lístek - orchestr", 1000, None, 0, "image", 1, False, 0,
        ],
        "data_predstavenie": [
            100, 10, start, start + 120, 0, "", 40, 0, 0, 0, 1, False, True, 0, 0, 0, 0, "",
            1, 1, 60, 120, 0, "", 0, 0, 0, 0, 1, False, True, 0, 0, 0, 0, "",
            1, 1, 60, 120, 0, "", 0, 0, 0, 0, 1, False, True, 0, 0, 0, 0, "",
        ],
    }
    lines = []
    for name, values in arrays.items():
        serialized = repr(values).replace("None", "null").replace("False", "false").replace("True", "true")
        lines.append(f"var {name} = {serialized};")
    return "\n".join(lines)


class TicketportalDecoderTests(unittest.TestCase):
    def test_decodes_delta_relationships_without_title_matching(self):
        cities = decode_cities([30, "Praha", 1, None])
        venues = decode_venues(
            [40, "Rudolfinum", 30, "", "", "", "", "", 0, 0],
            cities,
        )
        outputs = decode_outputs([
            50, "Same title", "first", "", "",
            1, "Same title", "second", "", "",
        ])
        events = decode_events([
            10, "Same title", 1000, [1008], 0, "", 50, False, 0,
            1, "Same title", 1000, [1008], 0, "", 1, False, 0,
        ])
        start = encoded_minute(2030, 1, 1, 19)
        performances = [
            100, 10, start, start + 60, 0, "", 40, 0, 0, 0, 1, False, True, 0, 0, 0, 0, "",
            1, 1, 60, 60, 0, "", 0, 0, 0, 0, 1, False, True, 0, 0, 0, 0, "",
        ]

        records = decode_occurrences(
            performances,
            events=events,
            outputs=outputs,
            venues=venues,
            selected_event_ids={10, 11},
            base_url="https://www.ticketportal.cz",
            timezone_name="Europe/Prague",
            today=date(2029, 1, 1),
        )

        self.assertEqual(
            [record["url"] for record in records],
            [
                "https://www.ticketportal.cz/event/first",
                "https://www.ticketportal.cz/event/second",
            ],
        )

    def test_converts_grid_minutes_to_local_time_across_dst(self):
        winter = encoded_minute(2030, 1, 1, 18)
        summer = encoded_minute(2030, 7, 1, 18)

        self.assertEqual(ticketportal_datetime(winter, "Europe/Prague").strftime("%H:%M"), "19:00")
        self.assertEqual(ticketportal_datetime(summer, "Europe/Prague").strftime("%H:%M"), "20:00")

    def test_auxiliary_filter_is_word_aware(self):
        patterns = TicketportalCzCrawler.site.auxiliary_patterns
        self.assertTrue(is_auxiliary_title("PARKOVACÍ LÍSTEK - koncert", patterns))
        self.assertTrue(is_auxiliary_title("Koncert: FAST TRACK O2", patterns))
        self.assertFalse(is_auxiliary_title("Parkovací kvartet", patterns))

    def test_live_film_filter_requires_context_performance_and_classical_force(self):
        self.assertTrue(is_live_film_music_event(
            "Film bude promítán a doprovodí jej živá hudba Howarda Shorea "
            "v podání 230členného orchestru a sborů."
        ))
        self.assertTrue(is_live_film_music_event(
            "Film bude živě hudebně doprovázen hamburským sborem sv. Mikuláše "
            "a varhanicí Patrycjou Olszewskou."
        ))
        self.assertFalse(is_live_film_music_event(
            "Projekce připomíná historii domu, do kterého se vrátila modlitebna "
            "Sboru Církve bratrské."
        ))
        self.assertFalse(is_live_film_music_event(
            "Filmová projekce s živým vystoupením DJ a následnou diskusí."
        ))


class TicketportalCrawlerTests(unittest.TestCase):
    def response(self, text):
        response = Mock()
        response.text = text
        response.raise_for_status.return_value = None
        return response

    def test_czech_crawler_unions_categories_and_keywords_and_enriches_once_per_url(self):
        crawler = TicketportalCzCrawler()
        detail = """
            <div class="detail-content"><h1><a href="/NEvent/ORGANIZER">Event</a></h1></div>
            <section class="popis"><div class="ticket-guarantee-container">Noise</div>Programme</section>
        """

        def get(url):
            return self.response(grid_fixture() if "/Grid/Data" in url else detail)

        with patch.object(crawler, "_get", side_effect=get) as request:
            records = crawler.scrape()

        self.assertEqual(len(records), 2)
        self.assertEqual({record["title"] for record in records}, {"Abonentní večer", "Komorní koncert"})
        self.assertTrue(all(record["time_from"] in {"20:00", "21:00"} for record in records))
        self.assertTrue(all(record["description"] == "Programme" for record in records))
        self.assertTrue(all(record["organizer_url"] == "https://www.ticketportal.cz/NEvent/ORGANIZER" for record in records))
        self.assertEqual(request.call_count, 3)

    @patch("crawlers.ticketportal.log_message")
    def test_detail_failure_keeps_grid_occurrences(self, log_message):
        crawler = TicketportalCzCrawler()

        def get(url):
            if "/Grid/Data" in url:
                return self.response(grid_fixture())
            raise RuntimeError("detail unavailable")

        with patch.object(crawler, "_get", side_effect=get):
            records = crawler.scrape()

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["description"] is None for record in records))
        self.assertEqual(log_message.call_count, 2)

    def test_missing_configured_category_fails_loudly(self):
        crawler = TicketportalSkCrawler()
        with patch.object(crawler, "_get", return_value=self.response(grid_fixture())):
            with self.assertRaisesRegex(ValueError, "Klasická hudba"):
                crawler._grid_records()

    def test_country_configs_remain_potential_sources(self):
        self.assertEqual(TicketportalSkCrawler.config.country_code, "SK")
        self.assertEqual(TicketportalCzCrawler.config.country_code, "CZ")
        self.assertEqual(TicketportalSkCrawler.config.upload_target, "potential")
        self.assertEqual(TicketportalCzCrawler.config.upload_target, "potential")

    def test_slovak_organizer_exclusion_is_preserved(self):
        crawler = TicketportalSkCrawler()
        frame = pd.DataFrame([
            {
                "title": "Excluded",
                "date": "2030-01-01",
                "time_from": "19:00",
                "venue": "Hall",
                "city": "Bratislava",
                "url": "https://example.com/excluded",
                "organizer_url": "https://www.ticketportal.sk/NEvent/SLOVENSKA_FILHARMONIA",
                "description": None,
            },
            {
                "title": "Kept",
                "date": "2030-01-01",
                "time_from": "19:00",
                "venue": "Hall",
                "city": "Bratislava",
                "url": "https://example.com/kept",
                "organizer_url": None,
                "description": None,
            },
        ])

        transformed = crawler.transform(frame)

        self.assertEqual(transformed["title"].tolist(), ["Kept"])


if __name__ == "__main__":
    unittest.main()
