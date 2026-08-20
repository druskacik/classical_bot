import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://albustanfestival.com/"
SOURCE = "Al Bustan Festival"
DEFAULT_VENUE = "Emile Bustani Auditorium"
DEFAULT_CITY = "Beit Mery"

HEADERS = {
    # The server returns 403 to requests' default user agent.
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# The venue page identifies the Emile Bustani Auditorium as the default and
# gives locations for the festival's recurring off-site venues.
VENUE_CITIES = (
    (("emile bustani", "al bustan hotel", "crystal garden"), "Beit Mery"),
    (("beyt meri", "beit mery", "beit meri", "mar sassine"), "Beit Mery"),
    (("sidon", "saida", "khan sacy", "khan el franj"), "Sidon"),
    (("tyre", "tyr"), "Tyre"),
    (("batroun", "smar jbeil", "kfarhay"), "Batroun"),
    (("byblos",), "Byblos"),
    (("zouk mikael",), "Zouk Mikael"),
    (("zouk mosbeh",), "Zouk Mosbeh"),
    (("jamhour",), "Jamhour"),
    (("balamand",), "Balamand"),
    (("jeita",), "Jeita"),
    (("baabda",), "Baabda"),
    (("faqra",), "Faqra"),
    (("jdeideh",), "Jdeideh"),
    (("maad",), "Maad"),
    (("debbyeh", "debbieh"), "Debbiyeh"),
    (
        (
            "beirut",
            "monot",
            "kantari",
            "ashrafieh",
            "achrafieh",
            "sursock",
            "aub",
            "a.u.b",
            "american university",
            "gemmayzeh",
            "gemayze",
            "clemenceau",
            "corniche mazraa",
            "national museum",
            "salle montaigne",
            "saint joseph",
            "st. joseph",
            "st joseph",
            "st elie",
            "saint-elie",
            "saint elie",
        ),
        "Beirut",
    ),
)


def _clean_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _session() -> requests.Session:
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


def _fetch_soup(session: requests.Session, url: str) -> BeautifulSoup:
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _discover_listing_urls(session: requests.Session) -> list[str]:
    soup = _fetch_soup(session, SOURCE_URL)
    urls = set()
    for anchor in soup.select("a[href]"):
        href = urljoin(SOURCE_URL, anchor.get("href", ""))
        path = urlparse(href).path.rstrip("/") + "/"
        text = anchor.get_text(" ", strip=True)
        if re.search(r"\bProgram(?:me)?\s+20\d{2}\b", text, re.I):
            urls.add(href)
        elif re.search(r"/(?:program-20\d{2}|20\d{2}-2)/$", path):
            urls.add(href)
        elif re.search(r"/side-event-20\d{2}/$", path):
            urls.add(href)
    return sorted(urls)


def _discover_detail_urls(
    session: requests.Session, listing_urls: list[str]
) -> list[str]:
    detail_urls = set()
    for listing_url in listing_urls:
        try:
            soup = _fetch_soup(session, listing_url)
        except requests.RequestException as error:
            log_message(
                "Listing page could not be fetched",
                event="crawler_url_fetch_failed",
                url=listing_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        for anchor in soup.select('a[href*="/blog/programme/"]'):
            detail_urls.add(urljoin(SOURCE_URL, anchor.get("href", "")))
    return sorted(detail_urls)


def _parse_date_time(raw: str) -> tuple[str, str | None]:
    raw = _clean_text(raw)
    match = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})"
        r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?)?",
        raw,
        re.I,
    )
    if not match:
        raise ValueError(f"Unrecognized event date: {raw!r}")

    event_date = datetime.strptime(match.group(1), "%d %B %Y").date().isoformat()
    if not match.group(2):
        return event_date, None

    hour = int(match.group(2))
    minute = int(match.group(3) or 0)
    meridiem = match.group(4).lower()
    if hour == 12:
        hour = 0
    if meridiem == "p":
        hour += 12
    return event_date, f"{hour:02d}:{minute:02d}"


def _city_for_venue(venue: str) -> str | None:
    normalized = venue.casefold()
    for needles, city in VENUE_CITIES:
        if any(needle in normalized for needle in needles):
            return city
    return None


def _description(article) -> str | None:
    parts = []
    details = article.select_one(".programme-details")
    if details:
        for node in details.select("h2, p"):
            text = _clean_text(node.get_text("\n", strip=True))
            if text:
                parts.append(text)

    programme = article.select_one("#tab1")
    if programme:
        text = _clean_text(programme.get_text("\n", strip=True))
        if text and text.casefold() not in {part.casefold() for part in parts}:
            parts.append(text)

    description = _clean_text("\n\n".join(parts))
    return description or None


def _parse_detail(soup: BeautifulSoup, url: str) -> dict | None:
    article = soup.select_one("article.performance")
    if not article:
        return None

    title_node = article.select_one(".programme-details h1, h1")
    date_node = article.select_one(".programme-details .date, .date")
    if not title_node or not date_node:
        return None

    title = _clean_text(title_node.get_text(" ", strip=True))
    if not title:
        return None

    date_text = date_node.get_text(" ", strip=True)
    event_date, time_from = _parse_date_time(date_text)
    venue_node = date_node.select_one(".highlight")
    venue = (
        _clean_text(venue_node.get_text(" ", strip=True))
        if venue_node
        else DEFAULT_VENUE
    )
    city = _city_for_venue(venue)
    if not city:
        log_message(
            "Skipping event with unmapped venue",
            event="crawler_record_skipped",
            url=url,
            venue=venue,
        )
        return None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": "LB",
        "description": _description(article),
    }


class AlBustanFestivalCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="albustanfestival_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="LB",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        listing_urls = _discover_listing_urls(session)
        detail_urls = _discover_detail_urls(session, listing_urls)
        records = []
        for url in detail_urls:
            try:
                record = _parse_detail(_fetch_soup(session, url), url)
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Event detail could not be parsed",
                    event="crawler_record_parse_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
        return records


def main():
    AlBustanFestivalCrawler().run()


if __name__ == "__main__":
    main()
