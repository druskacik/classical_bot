import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Resonance"
SOURCE_URL = "https://resonanceslo.com/"
ARCHIVE_URL = "https://resonanceslo.com/watch/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class ResonanceSloCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="resonanceslo_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    @staticmethod
    def _text(node):
        if node is None:
            return None
        value = node.get_text("\n", strip=True)
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip() or None

    @staticmethod
    def _parse_when(value):
        match = re.fullmatch(
            r"([A-Za-z]+ \d{1,2}, \d{4}) at "
            r"(\d{1,2}:\d{2} [ap]m)(?:\s*[–-]\s*(\d{1,2}:\d{2} [ap]m))?",
            value or "",
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        event_date = datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()

        def parse_time(raw):
            if not raw:
                return None
            return datetime.strptime(raw.upper(), "%I:%M %p").time().isoformat()

        return event_date, parse_time(match.group(2)), parse_time(match.group(3))

    @staticmethod
    def _parse_location(value):
        parts = [part.strip() for part in (value or "").split(",") if part.strip()]
        state_index = next(
            (
                index
                for index, part in enumerate(parts)
                if re.fullmatch(r"CA(?:\s+\d{5}(?:-\d{4})?)?", part, re.IGNORECASE)
            ),
            None,
        )
        if state_index is None or state_index < 2:
            return None

        venue = parts[0]
        city = parts[state_index - 1]
        if not venue or not city or venue.casefold() == city.casefold():
            return None
        return venue, city

    @classmethod
    def _parse_page(cls, html):
        soup = BeautifulSoup(html, "html.parser")
        records = []
        for item in soup.select(".widget_upcoming_events_widget .upcoming-events > li"):
            title = cls._text(item.select_one(".event-summary"))
            parsed_when = cls._parse_when(cls._text(item.select_one(".event-when")))
            parsed_location = cls._parse_location(cls._text(item.select_one(".event-location")))
            if not title or not parsed_when or not parsed_location:
                continue

            event_date, time_from, time_to = parsed_when
            venue, city = parsed_location
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": SOURCE_URL,
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "description": cls._text(item.select_one(".event-description")),
                }
            )
        return records

    @classmethod
    def _parse_archive(cls, html):
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one("article")
        if article is None:
            return []

        records = []
        for paragraph in article.select("p"):
            description = cls._text(paragraph)
            if not description or not re.search(r"\brecorded(?: live)?\b", description, re.IGNORECASE):
                continue

            title_match = re.search(r"[“\"]([^”\"]+)[”\"]", description)
            date_match = re.search(
                r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday,?\s+)?"
                r"([A-Za-z]+ \d{1,2})(?:st|nd|rd|th)?, (\d{4})",
                description,
                re.IGNORECASE,
            )
            location_match = re.search(
                r"\bat\s+(.+?)\s+in\s+([^,]+),\s*CA\b",
                description,
                re.IGNORECASE,
            )
            if not title_match or not date_match or not location_match:
                continue

            raw_date = f"{date_match.group(1)}, {date_match.group(2)}"
            try:
                event_date = datetime.strptime(raw_date, "%B %d, %Y").date().isoformat()
            except ValueError:
                continue

            venue = location_match.group(1).strip(" .")
            city = location_match.group(2).strip(" .")
            if not venue or not city or venue.casefold() == city.casefold():
                continue
            records.append(
                {
                    "title": title_match.group(1).strip(),
                    "date": event_date,
                    "url": ARCHIVE_URL,
                    "time_from": None,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "description": description,
                }
            )
        return records

    def scrape(self):
        records = []
        for url, parser in (
            (SOURCE_URL, self._parse_page),
            (ARCHIVE_URL, self._parse_archive),
        ):
            log_message("Fetching Resonance events", event="crawler_url_fetch", url=url)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            records.extend(parser(response.content))
        log_message(
            "Parsed Resonance events",
            event="crawler_page_parsed",
            record_count=len(records),
        )
        return records


def main():
    ResonanceSloCrawler().run()


if __name__ == "__main__":
    main()
