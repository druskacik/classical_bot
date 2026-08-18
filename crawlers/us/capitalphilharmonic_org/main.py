import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Capital Philharmonic of New Jersey"
SOURCE_URL = "https://www.capitalphilharmonic.org/"
CATALOG_URL = (
    "https://tickets.capitalphilharmonic.org/"
    "ticketing/capitalphil/catalog/concerts"
)
CHAMBER_URL = "https://www.capitalphilharmonic.org/chamber-concerts"
TICKETING_BASE = "https://tickets.capitalphilharmonic.org/ticketing/capitalphil/"
TIMEOUT = 30


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip(" \u200b")


def _parse_datetime(text):
    value = _clean(text)
    value = re.sub(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+", "", value)
    value = re.sub(r"(\d)(?:st|nd|rd|th)", r"\1", value)
    value = re.sub(r"\s+(?:EDT|EST)$", "", value)
    value = value.replace(",", "")
    value = value.replace(" at ", " ").replace(" • ", " ").replace(" · ", " ")
    value = re.sub(r"\s+", " ", value)
    for month_format in ("%b", "%B"):
        try:
            parsed = datetime.strptime(value, f"{month_format} %d %Y %I:%M %p")
            break
        except ValueError:
            parsed = None
    if parsed is None:
        raise ValueError(f"Unsupported event date: {text!r}")
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def _get(session, url):
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def _detail_fields(html, listed_venue):
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = [_clean(node.get_text(" ", strip=True)) for node in soup.select("p")]
    paragraphs = [text for text in paragraphs if text]

    date_index = next(
        (index for index, text in enumerate(paragraphs) if re.search(r"\b20\d{2}\s+at\s+\d", text)),
        None,
    )
    after_date = paragraphs[date_index + 1 :] if date_index is not None else paragraphs

    venue = listed_venue
    if "Chamber Music Series" in venue or "Musicans'Choice" in venue:
        venue = None
        for index, text in enumerate(after_date[:5]):
            if re.search(r"\b(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Trenton)\b", text, re.I):
                previous = after_date[index - 1] if index else ""
                if previous and len(previous) < 100:
                    venue = re.sub(r"\s+Confirmed$", "", previous, flags=re.I)
                    break

        if venue is None:
            detail_text = "\n".join(after_date)
            match = re.search(r"Set inside the ([^,.]+)", detail_text, re.I)
            if match:
                venue = _clean(match.group(1))

    description_parts = []
    for text in after_date:
        if "Choose your tickets and quantity below" in text:
            continue
        if text == "." or text == venue:
            continue
        if re.fullmatch(r"\d+\s+.*(?:NJ\s+\d{5}|Trenton).*", text, re.I):
            continue
        description_parts.append(text)

    return venue, "\n".join(description_parts) or None


def _scrape_catalog(session):
    soup = BeautifulSoup(_get(session, CATALOG_URL), "html.parser")
    records = []
    for card in soup.select(".custom-org-event-card"):
        title_node = card.select_one(".custom-org-catalog-concert-title")
        date_node = card.select_one(".custom-org-catalog-concert-date")
        venue_node = card.select_one(".custom-org-catalog-concert-venue span")
        button = card.select_one("button[data-testid^='buy-']")
        if not all((title_node, date_node, venue_node, button)):
            continue

        event_id = button["data-testid"].removeprefix("buy-")
        url = urljoin(TICKETING_BASE, event_id)
        try:
            date, time_from = _parse_datetime(date_node.get_text(" ", strip=True))
            listed_venue = _clean(venue_node.get_text(" ", strip=True))
            venue, description = _detail_fields(_get(session, url), listed_venue)
        except (requests.RequestException, ValueError) as error:
            log_message(
                "Skipping invalid concert detail",
                event="crawler_record_skipped",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        if not venue or "to be announced" in venue.lower():
            log_message(
                "Skipping concert without a confirmed venue",
                event="crawler_record_skipped",
                url=url,
            )
            continue

        records.append(
            {
                "title": _clean(title_node.get_text(" ", strip=True)),
                "date": date,
                "url": url,
                "time_from": time_from,
                "venue": venue,
                "city": "Trenton",
                "description": description,
            }
        )
    return records


def _archive_venue_and_city(location):
    location = _clean(location)
    if ", Ewing" in location:
        return location.split(",", 1)[0], "Ewing"
    if "•" in location or "·" in location:
        return re.split(r"\s*[•·]\s*", location, maxsplit=1)[0], "Trenton"
    venue = re.split(r"\s+\d+\s+", location, maxsplit=1)[0]
    return venue, "Trenton"


def _scrape_chamber_archive(session):
    soup = BeautifulSoup(_get(session, CHAMBER_URL), "html.parser")
    records = []
    date_pattern = re.compile(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June|July|"
        r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
        r"\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2}\s*(?:at|[•·])?\s*\d{1,2}:\d{2}\s*[AP]M",
        re.I,
    )

    seen = set()
    for container in soup.select("div.wixui-rich-text"):
        text = container.get_text("\n", strip=True)
        matches = list(date_pattern.finditer(text))
        if len(matches) != 1:
            continue
        match = matches[0]
        before = [_clean(part) for part in text[: match.start()].splitlines() if _clean(part)]
        after = [_clean(part) for part in text[match.end() :].splitlines() if _clean(part)]
        if not before or not after:
            continue

        title = before[-1]
        location = after[0]
        description = "\n".join(after[1:]) or None
        try:
            date, time_from = _parse_datetime(match.group())
            venue, city = _archive_venue_and_city(location)
        except ValueError:
            continue
        key = (title, date, venue)
        if not title or not venue or key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "title": title,
                "date": date,
                "url": CHAMBER_URL,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "description": description,
            }
        )
    return records


class CapitalPhilharmonicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="capitalphilharmonic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(
            {"User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"}
        )
        records = _scrape_catalog(session)
        records.extend(_scrape_chamber_archive(session))
        return records


def main():
    CapitalPhilharmonicCrawler().run()


if __name__ == "__main__":
    main()
