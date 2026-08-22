import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.nicoladibenedettoviolin.com/"
SOURCE = "Nicola Di Benedetto"
NEWS_URL = urljoin(SOURCE_URL, "notizie/")
TIMEOUT = 30

MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}

# The news archive has no location fields. These are exact, stable names used in
# the site's own titles and article bodies, not home-venue defaults.
LOCATIONS = (
    ("laguna park hotel", "Laguna Park Hotel", "Bibione", "IT"),
    ("teatro verdi di pordenone", "Teatro Verdi", "Pordenone", "IT"),
    ("teatro verdi pordenone", "Teatro Verdi", "Pordenone", "IT"),
    ("chiesa di santa maria maggiore, dardago", "Chiesa di Santa Maria Maggiore", "Dardago", "IT"),
    ("chiesa di s.maria maggiore", "Chiesa di Santa Maria Maggiore", "Vercelli", "IT"),
    ("ex convento di san francesco", "Ex Convento di San Francesco", "Pordenone", "IT"),
    ("doubletree by hilton trieste", "DoubleTree by Hilton Trieste", "Trieste", "IT"),
    ("casa tartini", "Casa Tartini", "Pirano", "SI"),
    ("palazzo attems", "Palazzo Attems", "Gorizia", "IT"),
    ("teatro lorenzo da ponte", "Teatro Lorenzo da Ponte", "Vittorio Veneto", "IT"),
    ("palazzo ragazzoni", "Palazzo Ragazzoni", "Sacile", "IT"),
    ("teatro gozzi", "Teatro Gozzi", "Pasiano di Pordenone", "IT"),
    ("villa frova", "Villa Frova", "Caneva", "IT"),
    ("palazzo montereale mantica", "Palazzo Montereale Mantica", "Pordenone", "IT"),
)

EVENT_SIGNALS = (
    "concerto",
    "concerti",
    "recital",
    "violinissimo",
    "quintetto",
    "festival",
    "duo di benedetto",
)
NON_EVENT_SIGNALS = (
    "intervista",
    "ospite su",
    "recording",
    "debut album",
    "primo premio",
    "vince anche",
    "grande successo",
    "capitale italiana della cultura",
    "recensione",
    "un’emozione unica",
    "due giovani virtuosi",
)


def _clean_text(node):
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _archive_urls():
    yield NEWS_URL
    offset = 10
    while True:
        yield urljoin(NEWS_URL, f"newscbm_733353/{offset}/")
        offset += 10


def _parse_date(text, published):
    candidates = []
    for numeric in re.finditer(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?!\d)", text):
        day, month, year = (int(value) for value in numeric.groups())
        year = year + 2000 if year < 100 else year
        try:
            candidates.append(datetime(year, month, day).date())
        except ValueError:
            pass

    for named in re.finditer(
        r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?",
        text,
        re.IGNORECASE,
    ):
        day = int(named.group(1))
        month = MONTHS[named.group(2).lower()]
        year = int(named.group(3) or published.year)
        try:
            candidates.append(datetime(year, month, day).date())
        except ValueError:
            pass

    if not candidates:
        return published.date().isoformat()
    return min(candidates, key=lambda value: abs(value - published.date())).isoformat()


def _parse_time(text, published, date_is_publication):
    match = re.search(
        r"(?:(?:ore|alle)\s*(\d{1,2})[:.]([0-5]\d)|(\d{1,2}):([0-5]\d))",
        text,
        re.IGNORECASE,
    )
    if match:
        hour = int(match.group(1) or match.group(3))
        minute = match.group(2) or match.group(4)
        if hour < 24:
            return f"{hour:02d}:{minute}"
    if not date_is_publication and published.time() != datetime.min.time():
        return None
    return published.strftime("%H:%M") if published.strftime("%H:%M") != "00:00" else None


def _location(text):
    lowered = text.lower()
    for marker, venue, city, country_code in LOCATIONS:
        if marker in lowered:
            return venue, city, country_code
    return None


def _is_candidate(title, description):
    text = f"{title} {description}".lower()
    lowered_title = title.lower()
    if any(signal in lowered_title for signal in NON_EVENT_SIGNALS):
        return False
    return any(signal in text for signal in EVENT_SIGNALS)


class NicolaDiBenedettoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nicoladibenedettoviolin_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IT",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "venue", "city"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-bot/1.0 (+concert listings crawler)"})
        article_urls = []

        for page_number, archive_url in enumerate(_archive_urls(), start=1):
            log_message("Fetching news archive", event="crawler_url_fetch", url=archive_url)
            response = session.get(archive_url, timeout=TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            page_urls = [
                urljoin(SOURCE_URL, anchor["href"])
                for anchor in soup.select("div.article h3 a[href]")
            ]
            new_urls = [url for url in page_urls if url not in article_urls]
            if not new_urls:
                break
            article_urls.extend(new_urls)
            if page_number >= 100:
                break

        records = []
        for url in article_urls:
            log_message("Fetching news detail", event="crawler_url_fetch", url=url)
            try:
                response = session.get(url, timeout=TIMEOUT)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                heading = soup.select_one("h1")
                container = heading.find_parent("div", class_="box") if heading else None
                published_node = container.select_one("ins") if container else None
                if heading is None or container is None or published_node is None:
                    continue

                title = _clean_text(heading)
                published = datetime.strptime(_clean_text(published_node), "%d.%m.%Y %H:%M")
                description_node = container.select_one("div.boxContent") or container
                description = _clean_text(description_node)
                description = re.sub(r"^\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s*", "", description)
                description = re.sub(r"\s*(?:Tag\s*:.*)?\s*Indietro\s*$", "", description).strip()

                combined = f"{title} {description}"
                location = _location(combined)
                if location is None or not _is_candidate(title, description):
                    continue

                date = _parse_date(title, published)
                if date == published.date().isoformat():
                    date = _parse_date(description, published)
                if date is None:
                    continue
                date_is_publication = date == published.date().isoformat()
                time_from = _parse_time(combined, published, date_is_publication)
                venue, city, country_code = location
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": url,
                        "time_from": time_from,
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": description or None,
                    }
                )
            except (requests.RequestException, ValueError) as error:
                log_message(
                    "Skipping unreadable news detail",
                    event="crawler_url_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message("Scrape completed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    NicolaDiBenedettoCrawler().run()


if __name__ == "__main__":
    main()
