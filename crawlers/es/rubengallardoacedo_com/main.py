import json
import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.rubengallardoacedo.com/en"
SOURCE = "Rubén Gallardo"
CATEGORY_URL = "https://www.rubengallardoacedo.com/noticias/categories/eventos"
TIMEOUT = 30

MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

# These are venue/city pairs explicitly and repeatedly identified by the site.
KNOWN_VENUES = (
    ("Basílica de Sant Francesc", "Palma"),
    ("Auditorium de Palma", "Palma"),
    ("Trui Teatre", "Palma"),
    ("Café a 3 Bandas", "Palma"),
    ("MediaMarkt FAN Mallorca", "Palma"),
    ("Rotllo de Sant Marçal", "Marratxí"),
)

EVENT_TERMS = re.compile(
    r"\b(conciert\w*|música en directo|orquesta|banda de música|"
    r"espectáculo musical|recital)\b",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"\b(?:lunes|martes|miércoles|jueves|viernes|sábado|domingo)?\s*"
    r"(?:día\s+)?(?P<day>\d{1,2})\s+de\s+"
    r"(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)"
    r"(?:\s+de\s+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b(?:a\s+las|comenzará\s+a\s+las|inicio(?:\s+previsto)?(?:\s+a\s+las)?)\s*"
    r"(?P<hour>[01]?\d|2[0-3])(?:[.:](?P<minute>[0-5]\d))?\s*(?:h(?:oras)?)?\b",
    re.IGNORECASE,
)


def _plain(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).casefold()


def _get(session: requests.Session, url: str) -> requests.Response:
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def _event_urls(session: requests.Session) -> list[str]:
    """Follow the first-party Eventos category's canonical pagination."""
    urls: list[str] = []
    page_url: str | None = CATEGORY_URL
    seen_pages: set[str] = set()

    while page_url and page_url not in seen_pages:
        seen_pages.add(page_url)
        soup = BeautifulSoup(_get(session, page_url).content, "html.parser")
        for link in soup.select('a[href*="/post/"]'):
            url = urljoin(page_url, link.get("href"))
            if url not in urls:
                urls.append(url)

        next_link = soup.select_one('link[rel="next"][href]')
        page_url = urljoin(page_url, next_link["href"]) if next_link else None

    return urls


def _published_date(soup: BeautifulSoup) -> date | None:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("datePublished"):
            continue
        try:
            return datetime.fromisoformat(data["datePublished"].replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            continue
    return None


def _event_date(text: str, published: date | None) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None

    day = int(match.group("day"))
    month = MONTHS[match.group("month").casefold()]
    if match.group("year"):
        years = [int(match.group("year"))]
    elif published:
        years = [published.year - 1, published.year, published.year + 1]
        years.sort(key=lambda year: abs((date(year, month, day) - published).days))
    else:
        years = [date.today().year]

    for year in years:
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def _time_from(text: str) -> str | None:
    match = TIME_RE.search(text)
    if not match:
        return None
    return f'{int(match.group("hour")):02d}:{int(match.group("minute") or 0):02d}'


def _venue_and_city(text: str) -> tuple[str | None, str | None]:
    normalized = _plain(text)
    for venue, city in KNOWN_VENUES:
        if _plain(venue) in normalized:
            return venue, city

    # Common wording in these posts: "<venue> de/en <city> acoge...".
    pattern = re.compile(
        r"\b(?P<venue>(?:Auditori|Auditorio|Teatre|Teatro|Basílica|Iglesia|"
        r"Església|Conservatorio|Centro Cultural|Centre Cultural|Sala)"
        r"[^.\n,;]{2,80}?)\s+(?:de|en)\s+"
        r"(?P<city>Palma(?: de Mallorca)?|Marratxí|Campos|Felanitx|Madrid)\b",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        return match.group("venue").strip(), match.group("city").strip()
    return None, None


def _parse_post(session: requests.Session, url: str) -> dict | None:
    soup = BeautifulSoup(_get(session, url).content, "html.parser")
    title_node = soup.select_one('h1[data-hook="post-title"]')
    body_node = soup.select_one('[data-hook="post-description"]')
    if not title_node or not body_node:
        return None

    title = title_node.get_text(" ", strip=True)
    description = body_node.get_text("\n", strip=True)
    evidence = f"{title}\n{description}"
    if not EVENT_TERMS.search(evidence):
        return None

    event_date = _event_date(description, _published_date(soup))
    venue, city = _venue_and_city(description)
    if not event_date or not venue or not city:
        return None

    return {
        "title": title,
        "date": event_date,
        "url": url,
        "time_from": _time_from(description),
        "venue": venue,
        "city": city,
        "description": description,
    }


class RubenGallardoAcedoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rubengallardoacedo_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="ES",
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = "ClassicalBot/1.0 (+concert research)"
        records = []
        for url in _event_urls(session):
            try:
                record = _parse_post(session, url)
            except requests.RequestException as error:
                log_message(
                    "Concert detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)

        log_message(
            "Candidate concert scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    RubenGallardoAcedoCrawler().run()


if __name__ == "__main__":
    main()
