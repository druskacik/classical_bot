import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://musicasacrany.com/"
SOURCE = "Musica Sacra"
ARCHIVE_URL = urljoin(SOURCE_URL, "past-performances/")
TIMEOUT = 30

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
DATE_RE = re.compile(
    r"(?i)\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*"
    r"(" + "|".join(MONTHS) + r")\s+"
    r"(\d{1,2}(?:\s*(?:,|&|and|–|—|-)\s*\d{1,2})*)"
    r"(?:\s*[,|–—-]\s*|\s+)(\d{4})\b"
)
TIME_RE = re.compile(r"(?i)\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?")
SEASON_RE = re.compile(r"\b(20\d{2})-(20\d{2})\s+Season\b", re.I)

NON_EVENT_PATHS = {
    "", "education", "listen", "watch", "recording", "support-musica-sacra",
    "volunteer", "about-musica-sacra", "music-director-kent-tritle",
    "asst-music-director-michael-sheetz", "board-of-directors", "contact",
    "past-performances",
}


def clean_text(value):
    if value is None:
        return None
    value = re.sub(r"[\t\r ]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "p":
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}"


def expand_dates(text, season=None):
    """Return all explicitly stated dates; ranges include both stated endpoints."""
    results = []
    for match in DATE_RE.finditer(text):
        month = MONTHS[match.group(1).lower()]
        days = [int(value) for value in re.findall(r"\d{1,2}", match.group(2))]
        year = int(match.group(3))
        if len(days) == 2 and re.search(r"[–—-]", match.group(2)):
            days = [days[0], days[1]]
        for day in days:
            try:
                results.append(date(year, month, day).isoformat())
            except ValueError:
                continue

    # Older archive entries frequently omit the year because it is supplied by
    # the enclosing season heading.
    if not results and season:
        no_year = re.search(
            r"(?i)\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\b", text
        )
        if no_year:
            month = MONTHS[no_year.group(1).lower()]
            year = season[0] if month >= 7 else season[1]
            try:
                results.append(date(year, month, int(no_year.group(2))).isoformat())
            except ValueError:
                pass
    return list(dict.fromkeys(results))


def infer_location(text, date_line_index):
    lines = [line.strip(" -–—|,") for line in text.splitlines() if line.strip()]
    venue = None
    city = None
    venue_words = re.compile(
        r"(?i)\b(?:hall|cathedral|church|chapel|theat(?:er|re)|auditorium|"
        r"college|university|center|centre|campus|home of)\b"
    )
    for line in lines[date_line_index + 1:date_line_index + 7]:
        lower = line.lower()
        if TIME_RE.search(line) or any(word in lower for word in (
            "conductor", "soprano", "tenor", "baritone", "chorus", "orchestra",
            "director", "piano", "organ", "cello", "mezzo", "tickets",
        )):
            continue
        if 2 < len(line) < 160 and venue_words.search(line) and not re.search(
            r"(?i)\b(?:concert will|program includes|program features|tickets? start|join us)\b",
            line,
        ):
            venue = line
            break

    if venue:
        venue = re.split(r",\s*\d+\s", venue, maxsplit=1)[0].strip()
        city_match = re.search(r",\s*([A-Za-z .'-]+),\s*(NY|MI|CT)\b", venue)
        if city_match:
            city = city_match.group(1).strip()
            venue = venue[:city_match.start()].strip(" ,")
    if not city:
        nearby = "\n".join(lines[max(0, date_line_index - 2):date_line_index + 5])
        if venue and any(name.lower() in venue.lower() for name in (
            "Carnegie Hall", "Cathedral of St. John the Divine", "Lincoln Center",
            "David Geffen Hall", "David H. Koch Theater", "Alice Tully Hall",
            "Church of St. Paul the Apostle", "Chapel of St. James",
        )):
            city = "New York"
        elif re.search(r"\bAnn Arbor\b", nearby, re.I):
            city = "Ann Arbor"
        elif re.search(r"\bStamford\b", nearby, re.I):
            city = "Stamford"
        elif re.search(r"\bPurchase(?: College)?\b", nearby, re.I):
            city = "Purchase"
    return venue, city


def title_before_date(lines, date_index, fallback):
    candidates = []
    for line in lines[max(0, date_index - 4):date_index]:
        line = line.strip()
        if not line or SEASON_RE.search(line) or line.lower() == "past performances":
            continue
        if len(line) <= 180:
            candidates.append(line)
    title = clean_text(candidates[-1] if candidates else fallback)
    if not title or re.search(
        r"(?i)^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b|"
        r"\b(?:conducted by|conductor|soprano|tenor|baritone|bass)\b",
        title,
    ) or re.match(r"^[,.;:]", title):
        return fallback
    return title


class MusicaSacraCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="musicasacrany_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})

    def fetch_soup(self, url):
        log_message("Fetching page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def discover_detail_urls(self, home_soup, archive_soup):
        urls = []
        for anchor in home_soup.select("a[href]"):
            label = clean_text(anchor.get_text(" ", strip=True)) or ""
            if not re.match(r"(?i)^(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", label):
                continue
            urls.append(urljoin(SOURCE_URL, anchor["href"]))

        # The archive's leading list links every newer event that has its own
        # page. Older seasons follow the first horizontal rule as inline text.
        for anchor in archive_soup.select("a[href]"):
            href = urljoin(ARCHIVE_URL, anchor["href"])
            parsed = urlparse(href)
            path = parsed.path.strip("/")
            label = clean_text(anchor.get_text(" ", strip=True)) or ""
            if parsed.netloc == urlparse(SOURCE_URL).netloc and path not in NON_EVENT_PATHS:
                if re.search(r"(?i)\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b", label):
                    urls.append(href.split("#", 1)[0])
        return list(dict.fromkeys(urls))

    def parse_detail(self, url):
        soup = self.fetch_soup(url)
        content = soup.select_one(".entry-content, article, main, .nk-page-content") or soup.body
        text = clean_text(content.get_text("\n", strip=True))
        if not text:
            return []
        title_node = content.select_one("h1") or soup.select_one("h1")
        fallback = clean_text(title_node.get_text(" ", strip=True)) if title_node else "Musica Sacra concert"
        lines = text.splitlines()
        candidates = [(i, line) for i, line in enumerate(lines) if expand_dates(line)]
        date_index = max(
            candidates,
            key=lambda item: (
                3 * bool(re.search(
                    r"(?i)\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                    item[1],
                ))
                + 2 * bool(TIME_RE.search(item[1]))
                + bool(re.match(r"(?i)^(?:" + "|".join(MONTHS) + r")\b", item[1]))
            ),
            default=(-1, ""),
        )[0]
        if date_index < 0:
            return []
        dates = expand_dates(lines[date_index])
        venue, city = infer_location(text, date_index)
        if not venue or not city:
            log_message("Skipping event with incomplete location", event="crawler_record_skipped", url=url)
            return []
        title = fallback
        time_from = parse_time(lines[date_index])
        return [{
            "title": title,
            "date": concert_date,
            "url": url,
            "time_from": time_from,
            "time_to": None,
            "venue": venue,
            "city": city,
            "description": text,
        } for concert_date in dates]

    def parse_inline_archive(self, archive_soup):
        content = archive_soup.select_one(".entry-content") or archive_soup.select_one("main")
        if not content:
            return []
        records = []
        season = None
        # Horizontal rules delimit the older archive's event descriptions.
        for node in content.find_all(["h3", "hr"], recursive=True):
            if node.name == "h3":
                match = SEASON_RE.search(node.get_text(" ", strip=True))
                if match:
                    season = (int(match.group(1)), int(match.group(2)))
                continue
            pieces = []
            sibling = node.next_sibling
            while sibling is not None and not (isinstance(sibling, Tag) and sibling.name == "hr"):
                if isinstance(sibling, Tag):
                    pieces.append(sibling.get_text("\n", strip=True))
                sibling = sibling.next_sibling
            text = clean_text("\n".join(pieces))
            if not text:
                continue
            if re.search(r"(?i)\b(?:virtual performance|digital concert|on-demand)\b", text):
                continue
            season_match = SEASON_RE.search(text)
            if season_match:
                season = (int(season_match.group(1)), int(season_match.group(2)))
            lines = text.splitlines()
            for date_index, date_line in enumerate(lines):
                dates = expand_dates(date_line, season)
                if not dates:
                    continue
                venue, city = infer_location(text, date_index)
                if not venue or not city:
                    continue
                title = title_before_date(lines, date_index, "Musica Sacra concert")
                for concert_date in dates:
                    records.append({
                        "title": title,
                        "date": concert_date,
                        "url": ARCHIVE_URL + (f"#{concert_date}"),
                        "time_from": parse_time(date_line),
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "description": text,
                    })
        return records

    def scrape(self):
        home_soup = self.fetch_soup(SOURCE_URL)
        archive_soup = self.fetch_soup(ARCHIVE_URL)
        records = []
        for url in self.discover_detail_urls(home_soup, archive_soup):
            try:
                records.extend(self.parse_detail(url))
            except requests.RequestException as error:
                log_message(
                    "Concert detail request failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        records.extend(self.parse_inline_archive(archive_soup))
        log_message("Scrape assembled", event="crawler_scrape_assembled", record_count=len(records))
        return records


def main():
    MusicaSacraCrawler().run()


if __name__ == "__main__":
    main()
