import html
import re
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://orchestrenouvellegeneration.com/"
SOURCE = "Orchestre Nouvelle Génération"
API_URL = urljoin(SOURCE_URL, "wp-json/wp/v2/pages")

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
DATE_RE = re.compile(
    rf"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*,?\s*"
    rf"(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<year>20\d{{2}})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])\.?m\.?(?=\b|\s|$)", re.I)
VENUE_RE = re.compile(
    r"\b(hall|salle|centre|center|church|église|eglise|espace|conservatoire|"
    r"theatre|théâtre|auditorium|cathedral|chapel|pollack|st\.?\s*jax)\b",
    re.IGNORECASE,
)
SEASON_RE = re.compile(r"^(?:season-)?20\d{2}(?:-20\d{2})?-(?:season|saison)$")


def _text(value: str) -> str:
    """Turn WordPress/Visual Composer content into useful lines of text."""
    value = html.unescape(value or "")
    value = re.sub(r"\[(?:/?vc_[^\]]+|/?rev_slider[^\]]*)\]", "\n", value)
    text = BeautifulSoup(value, "html.parser").get_text("\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _date(line: str) -> str | None:
    match = DATE_RE.search(line)
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            f"{match.group('month')} {match.group('day')} {match.group('year')}",
            "%B %d %Y",
        )
    except ValueError:
        return None
    return parsed.date().isoformat()


def _time(line: str) -> str | None:
    match = TIME_RE.search(line)
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm").lower() == "p":
        hour += 12
    return f"{hour:02d}:{int(match.group('minute') or 0):02d}"


def _venue(lines: list[str], date_index: int) -> str | None:
    # Calendars consistently put the venue immediately after the date. Limit the
    # search so that repertoire and performer prose cannot become a venue.
    for line in lines[date_index + 1 : date_index + 4]:
        cleaned = re.sub(r"\s*\((?:QC|Quebec|Québec)\)\s*$", "", line).strip(" -–|,")
        if len(cleaned) <= 120 and VENUE_RE.search(cleaned):
            return cleaned
    return None


def _event_url(raw: str, page_by_id: dict[int, dict]) -> str | None:
    # The page-id link is canonical WordPress data. Prefer it because several
    # old button shortcodes were copied with a stale URL from another concert.
    page_id = re.search(r"(?:page_id=|page_id%3D)(\d+)", raw)
    if page_id and int(page_id.group(1)) in page_by_id:
        return page_by_id[int(page_id.group(1))]["link"]
    encoded = re.search(r"url:(https?%3A%2F%2F[^|&\s\]]+)", html.unescape(raw), re.I)
    if encoded:
        url = unquote(encoded.group(1)).rstrip('"”\'')
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
            return url.split("?")[0].rstrip("/") + "/"
    return None


def _detail_venue(text: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _date(line):
            venue = _venue(lines, index)
            if venue:
                return venue
    # Older templates sometimes omit the date on the detail page but retain a
    # compact venue line near the beginning.
    for line in lines[:15]:
        cleaned = re.sub(r"\s*\((?:QC|Quebec|Québec)\)\s*$", "", line).strip(" -–|,")
        if len(cleaned) <= 120 and VENUE_RE.search(cleaned):
            return cleaned
    return None


def _fetch_pages() -> list[dict]:
    pages = []
    page_number = 1
    while True:
        response = requests.get(
            API_URL,
            params={
                "per_page": 100,
                "page": page_number,
                "_fields": "id,slug,link,title,content,parent",
            },
            timeout=30,
        )
        response.raise_for_status()
        pages.extend(response.json())
        if page_number >= int(response.headers.get("X-WP-TotalPages", "1")):
            return pages
        page_number += 1


class OrchestreNouvelleGenerationCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="orchestrenouvellegeneration_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CA",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        try:
            pages = _fetch_pages()
        except requests.RequestException as error:
            log_message(
                "Failed to fetch WordPress pages",
                event="crawler_url_fetch_failed",
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        page_by_id = {page["id"]: page for page in pages}
        page_by_url = {page["link"].rstrip("/") + "/": page for page in pages}
        records = []

        # Season pages are the first-party event feed. Each Visual Composer row
        # is one advertised occurrence and links to its concert detail page.
        season_pages = [page for page in pages if SEASON_RE.match(page["slug"])]
        for season in season_pages:
            raw = html.unescape(season["content"]["rendered"] or "")
            for row in re.split(r"\[vc_row[^\]]*\]", raw, flags=re.I)[1:]:
                row = row.split("[/vc_row]", 1)[0]
                lines = _text(row).splitlines()
                date_index = next((i for i, line in enumerate(lines) if _date(line)), None)
                if date_index is None:
                    continue

                event_url = _event_url(row, page_by_id)
                if not event_url:
                    continue

                detail = page_by_url.get(event_url)
                description = _text(detail["content"]["rendered"]) if detail else _text(row)
                venue = _venue(lines, date_index) or _detail_venue(description)
                if not venue:
                    continue

                headings = BeautifulSoup(row, "html.parser").find_all(re.compile(r"^h[1-5]$"))
                heading_texts = [heading.get_text(" ", strip=True) for heading in headings]
                title = next((value for value in heading_texts if value and not DATE_RE.search(value)), lines[0])
                if not title or DATE_RE.search(title):
                    continue

                records.append(
                    {
                        "title": html.unescape(title),
                        "date": _date(lines[date_index]),
                        "url": event_url,
                        "time_from": _time(lines[date_index]),
                        "time_to": None,
                        "venue": venue,
                        "city": "Montréal",
                        "description": description or None,
                    }
                )

        log_message(
            "Scraped concert occurrences",
            event="crawler_scrape_completed",
            url=API_URL,
            record_count=len(records),
        )
        return records


def main():
    OrchestreNouvelleGenerationCrawler().run()


if __name__ == "__main__":
    main()
