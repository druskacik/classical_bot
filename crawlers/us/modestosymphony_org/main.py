import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Modesto Symphony Orchestra"
SOURCE_URL = "https://www.modestosymphony.org/"
SITEMAP_URL = f"{SOURCE_URL}sitemap.xml"
CITY = "Modesto"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
}

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

OCCURRENCE_RE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2}),\s*(?P<year>\d{4})"
    r"(?:\s*(?:;|at)\s*(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m))?",
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_occurrence(value):
    match = OCCURRENCE_RE.search(clean_text(value))
    if not match:
        return None

    month = MONTHS[match.group("month").rstrip(".").casefold()]
    try:
        event_date = date(
            int(match.group("year")), month, int(match.group("day"))
        ).isoformat()
    except ValueError:
        return None

    time_from = None
    time_match = re.fullmatch(
        r"(\d{1,2})(?::(\d{2}))?\s*([ap])m", match.group("time") or "", re.I
    )
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).casefold() == "p":
            hour += 12
        minute = int(time_match.group(2) or 0)
        if hour < 24 and minute < 60:
            time_from = f"{hour:02d}:{minute:02d}"

    return event_date, time_from


def concert_urls(session):
    response = session.get(SITEMAP_URL, headers=HEADERS, timeout=45)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    urls = []
    for node in root.iter():
        if not node.tag.endswith("loc") or not node.text:
            continue
        url = node.text.strip()
        parsed = urlparse(url)
        if (
            parsed.scheme == "https"
            and parsed.netloc in {"modestosymphony.org", "www.modestosymphony.org"}
            and re.fullmatch(r"/concerts/[^/]+", parsed.path)
        ):
            urls.append(url)
    return sorted(set(urls))


def artistic_description(main):
    lines = [clean_text(line) for line in main.get_text("\n").splitlines()]
    lines = [line for line in lines if line]
    kept = []
    for line in lines:
        if re.match(
            r"^(?:Tickets starting|Tickets on sale|Reserved Lawn Seating)",
            line,
            re.IGNORECASE,
        ):
            break
        kept.append(line)
    return "\n".join(kept) or None


def parse_event(url, session=None):
    session = session or requests
    try:
        response = session.get(url, headers=HEADERS, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            "Concert detail request failed",
            event="crawler_detail_request_failed",
            level="warning",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.select_one("main")
    if not main:
        return []

    title_node = main.select_one("h1")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else "")

    venue = ""
    for line in main.get_text("\n").splitlines():
        line = clean_text(line)
        if line.startswith("📍"):
            venue = clean_text(line.removeprefix("📍").split(":", 1)[0]).title()
            break
    if not title or not venue:
        return []

    # The hero ticket buttons are the page's occurrence list. Parsing only the
    # first section avoids recommendation cards and duplicated ticket links.
    hero = main.select_one("section")
    occurrences = []
    for link in hero.select("a") if hero else []:
        parsed = parse_occurrence(link.get_text(" ", strip=True))
        if parsed and parsed not in occurrences:
            occurrences.append(parsed)

    description = artistic_description(main)
    return [
        {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "venue": venue,
            "city": CITY,
            "country_code": "US",
            "description": description,
            "source_url": SOURCE_URL,
            "source": SOURCE,
        }
        for event_date, time_from in occurrences
    ]


class ModestoSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="modestosymphony_org",
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
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = requests.Session()
        try:
            urls = concert_urls(session)
        except (requests.RequestException, ElementTree.ParseError) as error:
            log_message(
                "Concert sitemap request failed",
                event="crawler_listing_request_failed",
                level="error",
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(parse_event, url) for url in urls]
            for future in as_completed(futures):
                records.extend(future.result())

        log_message(
            "Concert sitemap parsed",
            event="crawler_scrape_completed",
            url=SITEMAP_URL,
            record_count=len(records),
        )
        return sorted(
            records,
            key=lambda item: (item["date"], item["time_from"] or "", item["title"]),
        )


def main():
    ModestoSymphonyOrgCrawler().run()


if __name__ == "__main__":
    main()
