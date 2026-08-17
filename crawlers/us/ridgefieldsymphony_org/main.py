import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Ridgefield Symphony Orchestra"
SOURCE_URL = "https://ridgefieldsymphony.org/"
TICKETS_URL = urljoin(SOURCE_URL, "buy-tickets-2026-2027")
CLIENT_ID = "35696"
API_ROOT = "https://web.ovationtix.com/trs/api/rest/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
API_HEADERS = {
    "Accept": "application/json",
    "clientid": CLIENT_ID,
    "newcirequest": "true",
    "Origin": "https://ci.ovationtix.com",
    "Referer": "https://ci.ovationtix.com/",
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def clean_text(value):
    if not value:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text or None


def ticket_references(html):
    soup = BeautifulSoup(html, "html.parser")
    references = []
    for link in soup.select('a[href*="ovationtix.com/35696/"]'):
        parsed = urlparse(link.get("href", ""))
        match = re.fullmatch(r"/35696/(production|performance)/(\d+)/?", parsed.path)
        if match:
            reference = match.groups()
            if reference not in references:
                references.append(reference)
    return references


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def event_title(production):
    description = clean_text(production.get("description"))
    if description:
        first_line = description.splitlines()[0]
        first_line = re.sub(r"^RSO\s+IN\s+CONCERT\s*:\s*", "", first_line, flags=re.I)
        if first_line and not re.match(r"^(SATURDAY|SUNDAY|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY)\b", first_line, re.I):
            return first_line.strip(" -")

    title = clean_text(production.get("productionName"))
    if not title:
        return None
    title = re.sub(r"^\d+\s+", "", title)
    title = re.sub(r"\s+-\s+(?:Saturday|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday)?\s*\d{1,2}[./]\d{1,2}[./]\d{2,4}$", "", title, flags=re.I)
    return title.strip(" -") or None


def production_records(payload, public_url):
    production = payload.get("production") if "production" in payload else payload
    if not isinstance(production, dict):
        return []

    title = event_title(production)
    description = clean_text(production.get("description"))
    venue_data = production.get("venue") or {}
    address = venue_data.get("address") or {}
    venue = clean_text(venue_data.get("name"))
    city = clean_text(address.get("city"))
    country_code = clean_text(address.get("countryAbbrev") or address.get("country"))
    if not title or not venue or not city or country_code != "US":
        return []

    performances = production.get("performances") or []
    if payload.get("startDate") and not performances:
        performances = [payload]

    records = []
    for performance in performances:
        start = parse_datetime(performance.get("startDate"))
        if not start:
            continue
        end = parse_datetime(performance.get("endDate"))
        records.append(
            {
                "title": title,
                "date": start.date().isoformat(),
                "url": public_url,
                "time_from": start.strftime("%H:%M"),
                "time_to": end.strftime("%H:%M") if end else None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
                "source_url": SOURCE_URL,
                "source": SOURCE,
            }
        )
    return records


class RidgefieldSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ridgefieldsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = make_session()
        try:
            log_message("Fetching ticket listing", event="crawler_url_fetch", url=TICKETS_URL)
            response = session.get(TICKETS_URL, timeout=45)
            response.raise_for_status()
            references = ticket_references(response.text)

            records = []
            for reference_type, identifier in references:
                if reference_type == "production":
                    api_url = f"{API_ROOT}Production({identifier})/performance?"
                else:
                    api_url = f"{API_ROOT}Performance({identifier})"
                public_url = f"https://ci.ovationtix.com/{CLIENT_ID}/{reference_type}/{identifier}"
                try:
                    api_response = session.get(api_url, headers=API_HEADERS, timeout=60)
                    api_response.raise_for_status()
                    records.extend(production_records(api_response.json(), public_url))
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Failed to fetch ticket production",
                        event="crawler_detail_request_failed",
                        level="warning",
                        url=api_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

            log_message(
                "Ticket productions parsed",
                event="crawler_scrape_completed",
                url=TICKETS_URL,
                record_count=len(records),
            )
            return records
        finally:
            session.close()


def main():
    RidgefieldSymphonyOrgCrawler().run()


if __name__ == "__main__":
    main()
