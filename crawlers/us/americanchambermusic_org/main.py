import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "American Chamber Music Society"
SOURCE_URL = "https://www.americanchambermusic.org/"
CONCERTS_URL = "https://www.americanchambermusic.org/concerts"

DATE_TIME_RE = re.compile(
    r"^(?P<date>[A-Z]+\s+\d{1,2},\s+\d{4})\s*\|\s*(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)$",
    re.IGNORECASE,
)


def _clean_lines(container) -> list[str]:
    lines = []
    for line in container.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line.replace("\u200b", "")).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return lines


def _parse_event(container) -> dict | None:
    lines = _clean_lines(container)
    date_index = next(
        (index for index, line in enumerate(lines) if DATE_TIME_RE.fullmatch(line)),
        None,
    )
    if date_index is None or date_index == 0 or len(lines) <= date_index + 3:
        return None

    match = DATE_TIME_RE.fullmatch(lines[date_index])
    try:
        event_date = datetime.strptime(match.group("date"), "%B %d, %Y").date().isoformat()
        time_from = datetime.strptime(
            re.sub(r"\s+", "", match.group("time")), "%I%p"
            if ":" not in match.group("time")
            else "%I:%M%p",
        ).time().strftime("%H:%M")
    except ValueError:
        return None

    title = lines[date_index - 1]
    venue = lines[date_index + 1]
    city_line = lines[date_index + 2]
    city_match = re.fullmatch(r"(.+?),\s*[A-Z]{2}", city_line)
    if not title or not venue or not city_match:
        return None

    description_lines = lines[date_index + 3 :]
    description = "\n".join(description_lines) or None
    anchor = container.get("id")

    return {
        "title": title,
        "date": event_date,
        "url": f"{CONCERTS_URL}#{anchor}" if anchor else CONCERTS_URL,
        "time_from": time_from,
        "venue": venue,
        "city": city_match.group(1).strip(),
        "description": description,
    }


class AmericanChamberMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="americanchambermusic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert listing", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for container in soup.select("div.gpDCD5"):
            record = _parse_event(container)
            if record is not None:
                records.append(record)

        log_message(
            "Concert listing parsed",
            event="crawler_scrape_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    AmericanChamberMusicCrawler().run()


if __name__ == "__main__":
    main()
