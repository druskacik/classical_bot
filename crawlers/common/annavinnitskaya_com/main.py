import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Anna Vinnitskaya"
SOURCE_URL = "https://annavinnitskaya.com/en/home"
CONCERTS_URL = "https://annavinnitskaya.com/en/concerts/ajax"

COUNTRY_CODES = {
    "Austria": "AT",
    "Belgium": "BE",
    "Czech Republic": "CZ",
    "Germany": "DE",
    "Switzerland": "CH",
    "United Kingdom": "GB",
}

HEADING_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s*/\s*"
    r"(?P<city>[^/]+?)\s*,\s*(?P<country>[^/]+?)\s*/\s*"
    r"(?P<time>\d{1,2}:\d{2})$"
)


def _clean_text(element) -> str:
    return re.sub(r"\n{3,}", "\n\n", element.get_text("\n", strip=True)).strip()


def _parse_concert(container) -> dict | None:
    heading = container.find("h3")
    details = heading.find_next_sibling("div") if heading else None
    if details is None:
        return None

    match = HEADING_RE.match(heading.get_text(" ", strip=True))
    if not match:
        return None

    try:
        concert_date = datetime.strptime(match.group("date"), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None

    country_code = COUNTRY_CODES.get(match.group("country").strip())
    venue_element = details.find("em")
    venue = venue_element.get_text(" ", strip=True) if venue_element else ""
    city = match.group("city").strip()
    if not country_code or not city or not venue:
        return None

    paragraphs = [
        _clean_text(paragraph)
        for paragraph in details.find_all("p")
        if paragraph.find("em") is None and _clean_text(paragraph)
    ]
    description = "\n\n".join(paragraphs) or None
    billing = paragraphs[0].splitlines()[0] if paragraphs else "Piano concert"

    ticket_link = details.find("a", href=True)
    url = urljoin(SOURCE_URL, ticket_link["href"]) if ticket_link else SOURCE_URL

    return {
        "title": f"Anna Vinnitskaya – {billing}",
        "date": concert_date,
        "url": url,
        "time_from": match.group("time"),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    }


class AnnaVinnitskayaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="annavinnitskaya_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        skipped_count = 0
        for container in soup.select(".accordion > .container"):
            record = _parse_concert(container)
            if record is None:
                skipped_count += 1
                continue
            records.append(record)

        log_message(
            "Concert calendar parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            skipped_count=skipped_count,
            url=CONCERTS_URL,
        )
        return records


def main():
    AnnaVinnitskayaCrawler().run()


if __name__ == "__main__":
    main()
