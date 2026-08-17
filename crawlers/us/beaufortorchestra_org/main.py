import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Beaufort Symphony Orchestra"
SOURCE_URL = "https://www.beaufortorchestra.org/"
SCHEDULE_URL = f"{SOURCE_URL}schedule.html"
DEFAULT_CITY = "Beaufort"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) HeadlessChrome/151.0.0.0 Safari/537.36"
    ),
    "Upgrade-Insecure-Requests": "1",
    "Sec-CH-UA": '"Chromium";v="151", "Not=A?Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Linux"',
}

EVENT_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*:\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
    r"(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4}),\s*"
    r"(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)\s*,?\s*at\s+(?P<venue>.+?)\.?$",
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ""
    return " ".join(str(value).replace("\xa0", " ").split())


def parse_schedule(html):
    soup = BeautifulSoup(html, "html.parser")
    descriptions = [clean_text(node.get_text(" ", strip=True)) for node in soup.select("h3.numb")]
    records = []

    # These summary rows are the schedule's authoritative occurrence list. They
    # also avoid a known stale year typo in one of the decorative detail rows.
    for node in soup.select("p.lg.wind"):
        match = EVENT_PATTERN.match(clean_text(node.get_text(" ", strip=True)))
        if not match:
            continue

        title = clean_text(match.group("title"))
        venue = re.sub(r"^the\s+", "", clean_text(match.group("venue")), flags=re.IGNORECASE)
        try:
            date = datetime.strptime(match.group("date"), "%B %d, %Y").date().isoformat()
            time_from = datetime.strptime(match.group("time").upper(), "%I:%M %p").strftime("%H:%M")
        except ValueError as error:
            log_message(
                "Skipping concert with invalid date or time",
                event="crawler_record_skipped",
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        description = next(
            (text for text in descriptions if text.casefold().startswith(title.casefold())),
            None,
        )
        records.append(
            {
                "title": title,
                "date": date,
                "url": SCHEDULE_URL,
                "time_from": time_from,
                "venue": venue,
                "city": DEFAULT_CITY,
                "country_code": "US",
                "description": description,
                "source_url": SOURCE_URL,
                "source": SOURCE,
            }
        )

    return records


class BeaufortorchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="beaufortorchestra_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        columns=[
            "title",
            "date",
            "url",
            "time_from",
            "venue",
            "city",
            "country_code",
            "description",
            "source_url",
            "source",
        ],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self):
        log_message("Fetching concert schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        response.encoding = "utf-8"
        records = parse_schedule(response.text)
        log_message(
            "Concert schedule parsed",
            event="crawler_scrape_completed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    BeaufortorchestraOrgCrawler().run()


if __name__ == "__main__":
    main()
