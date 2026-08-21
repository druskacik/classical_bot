import re
from datetime import date, datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Dominic Miller"
SOURCE_URL = "https://dominicmiller.com/"
TOURS_URL = "https://dominicmiller.com/tours/"
API_URL = "https://dominicmiller.com/wp-json/wp/v2/pages"

COUNTRY_CODES = {
    "Cyprus": "CY",
    "Serbia": "RS",
    "Turkey": "TR",
    "United Kingdom": "GB",
}

WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "fr": 4,
    "sat": 5,
    "sun": 6,
}

DATE_RE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Fr|Sat|Sun),\s+([A-Z][a-z]{2})\s+(\d{1,2})$"
)
TIME_RE = re.compile(r"Start\s*:\s*(\d{1,2}:\d{2})", re.IGNORECASE)


def _infer_date(value: str, today: date | None = None) -> str | None:
    """Resolve the site's yearless date using its printed weekday."""
    match = DATE_RE.fullmatch(value.strip())
    if not match:
        return None

    weekday, month, day = match.groups()
    today = today or date.today()
    candidates = []
    for year in range(today.year - 3, today.year + 4):
        try:
            candidate = datetime.strptime(f"{year} {month} {day}", "%Y %b %d").date()
        except ValueError:
            continue
        if candidate.weekday() == WEEKDAYS[weekday.lower()]:
            candidates.append(candidate)

    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs((candidate - today).days)).isoformat()


def _button_url(container, label: str) -> str | None:
    for button in container.select("a.elementor-button"):
        text = button.get_text(" ", strip=True)
        if text.casefold() == label.casefold() and button.get("href"):
            return button["href"]
    return None


def _event_url(container) -> str:
    event_url = _button_url(container, "Event")
    if event_url:
        parsed = urlparse(event_url)
        is_generic_facebook = parsed.netloc.endswith("facebook.com") and "/events/" not in parsed.path
        if parsed.path.strip("/") and not is_generic_facebook:
            return event_url
    return _button_url(container, "Tickets") or TOURS_URL


def _parse_event(container, date_text: str) -> dict | None:
    event_date = _infer_date(date_text)
    heading = container.select_one("h4.ekit-heading--title")
    description_box = container.select_one(".ekit-heading__description")
    if not event_date or not heading or not description_box:
        return None

    heading_text = heading.get_text(" ", strip=True)
    lines = [p.get_text(" ", strip=True) for p in description_box.select("p")]
    location = next((line for line in lines if not TIME_RE.search(line)), "")
    if "," not in location:
        return None
    location_name, country_name = [part.strip() for part in location.rsplit(",", 1)]
    country_code = COUNTRY_CODES.get(country_name)
    if not country_code:
        return None

    address_element = container.select_one(".elementor-icon-list-text")
    address = address_element.get_text(" ", strip=True) if address_element else ""
    city = location_name
    if "|" in address:
        city = address.rsplit("|", 1)[1].strip()
        city = re.sub(r",?\s+(?:CY|SRB|TUR|UK)$", "", city, flags=re.IGNORECASE).strip()

    # Most cards put the city in the location line and the venue in the
    # heading. The Cyprus festival card instead puts its venue in that line.
    venue = heading_text
    if "festival" in heading_text.casefold() and location_name.casefold() != city.casefold():
        venue = location_name

    time_match = next((TIME_RE.search(line) for line in lines if TIME_RE.search(line)), None)
    event_url = _event_url(container)
    description_parts = [f"{SOURCE} live at {venue}.", location]
    if address:
        description_parts.append(address)

    if not all((heading_text, event_date, event_url, venue, city)):
        return None
    return {
        "title": f"{SOURCE} — {heading_text}",
        "date": event_date,
        "url": event_url,
        "time_from": time_match.group(1) if time_match else None,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": "\n".join(description_parts),
    }


class DominicMillerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dominicmiller_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "time_from", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching tour calendar", event="crawler_url_fetch", url=API_URL)
        response = requests.get(
            API_URL,
            params={"slug": "tours", "context": "view"},
            timeout=30,
        )
        response.raise_for_status()
        pages = response.json()
        if not pages:
            log_message("Tours page was not returned", event="crawler_empty_response", url=API_URL)
            return []

        soup = BeautifulSoup(pages[0]["content"]["rendered"], "html.parser")
        records = []
        for text_widget in soup.select(".elementor-widget-text-editor"):
            date_text = text_widget.get_text(" ", strip=True)
            if not DATE_RE.fullmatch(date_text):
                continue
            date_container = text_widget.find_parent("div", class_="e-con")
            event_container = date_container.find_parent("div", class_="e-con") if date_container else None
            record = _parse_event(event_container, date_text) if event_container else None
            if record:
                records.append(record)
            else:
                log_message(
                    "Skipping incomplete tour listing",
                    event="crawler_record_skipped",
                    url=TOURS_URL,
                )
        return records


def main():
    DominicMillerCrawler().run()


if __name__ == "__main__":
    main()
