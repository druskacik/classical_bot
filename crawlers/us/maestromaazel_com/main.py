import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.maestromaazel.com/"
SOURCE = "Maestro Lorin Maazel"


class MaestroMaazelCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="maestromaazel_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching events page", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # The site presents its current or most recent performance in a hero block:
        # "Title | City, ST | Month D, YYYY", followed by descriptive copy and
        # an INFO & TICKETS link.  It has no calendar or paginated archive.
        page_text = soup.get_text("\n", strip=True)
        heading_pattern = re.compile(
            r"(?P<title>[^\n|]+?)\s*\|\s*"
            r"(?P<city>[^\n,|]+),\s*(?P<state>[A-Z]{2})\s*\|\s*"
            r"(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})"
        )
        match = heading_pattern.search(page_text)
        if not match:
            log_message("No dated performance found", event="crawler_no_events", url=SOURCE_URL)
            return []

        try:
            event_date = datetime.strptime(match.group("date"), "%B %d, %Y").date().isoformat()
        except ValueError as error:
            log_message(
                "Skipping performance with invalid date",
                event="crawler_record_skipped",
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            return []

        remainder = page_text[match.end():]
        description = remainder.split("INFO & TICKETS", 1)[0].strip() or None
        venue = self._extract_venue(description)
        if not venue:
            log_message(
                "Skipping performance without a defensible venue",
                event="crawler_record_skipped",
                url=SOURCE_URL,
            )
            return []

        return [{
            "title": match.group("title").strip(),
            "date": event_date,
            "url": SOURCE_URL,
            "time_from": None,
            "venue": venue,
            "city": match.group("city").strip(),
            "description": description,
        }]

    @staticmethod
    def _extract_venue(description: str | None) -> str | None:
        if not description:
            return None
        match = re.search(
            r"(?:in|at)\s+Castleton(?:[’']s)?\s+(?:intimate\s+)?([^.!?]*?(?:theat(?:er|re)|hall|auditorium))",
            description,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        venue = re.sub(r"\s+", " ", match.group(1)).strip()
        return f"Castleton {venue.title()}"


def main():
    MaestroMaazelCrawler().run()


if __name__ == "__main__":
    main()
