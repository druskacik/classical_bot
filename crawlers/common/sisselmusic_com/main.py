import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://sisselmusic.com/concerts/"
SOURCE = "Sissel Music"
AJAX_URL = "https://sisselmusic.com/wp-admin/admin-ajax.php"

COUNTRY_CODES = {
    "denmark": "DK",
    "dk": "DK",
    "faroe islands": "FO",
    "fi": "FI",
    "fo": "FO",
    "iceland": "IS",
    "island": "IS",
    "no": "NO",
    "norway": "NO",
    "se": "SE",
    "sweden": "SE",
    "usa": "US",
}

STATUS_PART = re.compile(
    r"^(?:ekstra(?:konsert|\s+konsert)?|få billetter|utsolgt|sold out|"
    r"med gjest\b.*|tickets?\b.*|nb:.*)$",
    re.IGNORECASE,
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip(" ,-\u00a0")


def _parse_location(value, title=""):
    """Return venue (when embedded), city, and ISO country from a location line."""
    value = _clean(value)
    country_code = None
    match = re.search(
        r"(?:,\s*|\s+-\s*)(Denmark|Faroe Islands|Iceland|Island|Norway|Sweden|USA|DK|FI|FO|NO|SE)\b",
        value,
        re.IGNORECASE,
    )
    country_label = match.group(1).lower() if match else None
    if match:
        country_code = COUNTRY_CODES[country_label]
        value = _clean(value[: match.start()])

    if not country_code:
        return None, None, None

    parts = [_clean(part) for part in value.split(",") if _clean(part)]
    if len(parts) >= 2:
        return ", ".join(parts[:-1]), parts[-1], country_code
    if parts and country_label == "island" and re.search(re.escape(parts[0]), title, re.IGNORECASE):
        return parts[0], None, country_code
    return None, parts[0] if parts else None, country_code


def _clean_venue(value):
    value = _clean(value)
    value = re.sub(
        r"\s*(?:(?:,|\s+-)\s*)?(?:\((?:ekstra(?:konsert|\s+konsert)?|utsolgt|sold out)\)|"
        r"ekstra(?:konsert|\s+konsert)?|få billetter|utsolgt|sold out|nb:.*)$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return _clean(value)


def _venue_from_title(title):
    title = _clean(title)
    parts = [_clean(part) for part in re.split(r"\s+-\s+|\s*//\s*", title)]
    parts = [part for part in parts if part and not STATUS_PART.match(part)]

    if len(parts) > 1:
        candidate = parts[-1]
        candidate = re.sub(r"^and\s+", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+med\s+(?:Sissel|gjest).*$", "", candidate, flags=re.IGNORECASE)
        # A phrase describing collaborators or a festival is not a venue.
        if not re.search(r"\b(?:orchestra|orkester|symfoniorkester|musikkfest)\b", candidate, re.I):
            return _clean_venue(candidate)

    comma_parts = [_clean(part) for part in title.split(",")]
    if len(comma_parts) > 1 and not STATUS_PART.match(comma_parts[1]):
        return _clean_venue(comma_parts[1])

    at_match = re.search(r"\bpå\s+(.+?)\s+med\b", title, re.IGNORECASE)
    if at_match:
        return _clean_venue(at_match.group(1))

    # Older archive rows use the venue itself as the event title.
    if not re.search(r"\b(?:konsert|concert|sissel|reflections|sommer|jul|festival)\b", title, re.I):
        return _clean_venue(title)
    return None


def _parse_events(html, year, fallback_url=SOURCE_URL):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for event in soup.select(".event-line"):
        title_node = event.select_one(".event-line__content h3")
        day_node = event.select_one(".event-line__date .day")
        month_node = event.select_one(".event-line__date .month")
        details = event.select(".event-line__content div > span")
        if not title_node or not day_node or not month_node or len(details) < 2:
            continue

        title = _clean(title_node.get_text(" ", strip=True))
        try:
            event_date = datetime.strptime(
                f"{day_node.get_text(strip=True)} {month_node.get_text(strip=True)} {year}",
                "%d %b %Y",
            ).date().isoformat()
        except ValueError:
            continue

        location_venue, city, country_code = _parse_location(
            details[1].get_text(" ", strip=True), title
        )
        venue = _clean_venue(location_venue or _venue_from_title(title))
        if not city or not country_code or not venue:
            continue

        link = event.select_one(".event-line__link a[href]")
        url = _clean(link.get("href")) if link else fallback_url
        if not url.startswith(("http://", "https://")):
            url = fallback_url

        time_from = _clean(details[0].get_text(" ", strip=True)) or None
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_from or ""):
            time_from = None

        records.append(
            {
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": None,
            }
        )
    return records


class SisselMusicCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sisselmusic_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-events-crawler/1.0"})
        log_message("Fetching concert calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        page = BeautifulSoup(response.text, "html.parser")

        records = []
        upcoming = page.select_one("section.concerts")
        year_node = page.select_one("section.concerts .period")
        if upcoming and year_node:
            year_match = re.search(r"\b(20\d{2})\b", year_node.get_text(" ", strip=True))
            if year_match:
                records.extend(_parse_events(str(upcoming), int(year_match.group(1))))

        years = sorted(
            {
                int(button["data-year"])
                for button in page.select(".concerts-archive [data-year]")
                if button.get("data-year", "").isdigit()
            }
        )
        for year in years:
            log_message(
                "Fetching concert archive year",
                event="crawler_url_fetch",
                url=AJAX_URL,
                year=year,
            )
            archive_response = session.get(
                AJAX_URL,
                params={"action": "getconcertsbyyear", "year": year},
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=30,
            )
            archive_response.raise_for_status()
            payload = archive_response.json()
            records.extend(_parse_events(payload.get("html", ""), year))

        log_message(
            "Parsed concert calendar",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    SisselMusicCrawler().run()


if __name__ == "__main__":
    main()
