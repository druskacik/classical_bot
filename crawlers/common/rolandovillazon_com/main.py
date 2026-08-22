import json
import re
import time
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Rolando Villazón"
SOURCE_URL = "https://rolandovillazon.com/"
LOAD_MORE_URL = urljoin(
    SOURCE_URL, "wp-content/themes/rolando-villazon/load-more.php"
)
FEEDS = ("singing/", "directing/")
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
MONTH_PATTERN = re.compile(
    rf"\b({'|'.join(MONTHS)})\b", re.IGNORECASE
)

# The calendar is a touring artist's schedule and does not expose country fields.
# These first-party location labels are stable venue/city evidence seen in its
# current and archived feeds. Unknown locations are deliberately skipped.
LOCATION_RULES = (
    ("stolpe an der peene", "Stolpe an der Peene", "Haferscheune des Gutshauses", "DE"),
    ("schloss esterházy", "Eisenstadt", "Schloss Esterházy", "AT"),
    ("bad wörishofen", "Bad Wörishofen", "Kurhaus Bad Wörishofen", "DE"),
    ("st.gallen", "St. Gallen", "Konzert und Theater St. Gallen", "CH"),
    ("st. gallen", "St. Gallen", "Konzert und Theater St. Gallen", "CH"),
    ("vienna state opera", "Vienna", "Vienna State Opera", "AT"),
    ("metropolitan opera", "New York City", "The Metropolitan Opera", "US"),
    ("musiktheater linz", "Linz", "Musiktheater Linz", "AT"),
    ("kölner philharmonie", "Cologne", "Kölner Philharmonie", "DE"),
    ("konzerthaus wien", "Vienna", "Konzerthaus Wien", "AT"),
    ("prinzregententheater", "Munich", "Prinzregententheater", "DE"),
    ("elbphilharmonie", "Hamburg", "Elbphilharmonie", "DE"),
    ("liederhalle", "Stuttgart", "Liederhalle", "DE"),
    ("tonhalle zürich", "Zurich", "Tonhalle Zürich", "CH"),
    ("staatsoper unter den linden", "Berlin", "Staatsoper Unter den Linden", "DE"),
    ("berlin state opera", "Berlin", "Berlin State Opera Unter den Linden", "DE"),
    ("kloster eberbach", "Eltville am Rhein", "Kloster Eberbach, Basilika", "DE"),
    ("wasserschloss raesfeld", "Raesfeld", "Wasserschloss Raesfeld", "DE"),
    ("musik- und kongresshalle", "Lübeck", "Musik- und Kongresshalle Lübeck", "DE"),
    ("deutsches haus", "Flensburg", "Deutsches Haus", "DE"),
    ("festival paax", "Playa del Carmen", "Festival PAAX GNP", "MX"),
    ("deusche oper berlin", "Berlin", "Deutsche Oper Berlin", "DE"),
    ("deutsche oper berlin", "Berlin", "Deutsche Oper Berlin", "DE"),
    ("haus für mozart", "Salzburg", "Haus für Mozart", "AT"),
    ("semperoper", "Dresden", "Semperoper Dresden", "DE"),
    ("opéra de monte-carlo", "Monte Carlo", "Opéra de Monte-Carlo", "MC"),
    ("stiftung mozarteum", "Salzburg", "Stiftung Mozarteum", "AT"),
    ("felsenreitschule", "Salzburg", "Felsenreitschule", "AT"),
    ("mozart residence", "Salzburg", "Mozart Residence", "AT"),
)


def _get(session, url, **kwargs):
    for attempt in range(3):
        try:
            response = session.get(url, timeout=40, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            if attempt == 2:
                raise
            log_message(
                "Retrying calendar request",
                event="crawler_url_retry",
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            time.sleep(1 + attempt)


def _flatten(value, prefix=""):
    """Match jQuery.param(), which is used by the site's Load More button."""
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}[{key}]" if prefix else key
            result.extend(_flatten(child, name))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_flatten(child, f"{prefix}[{index}]"))
    elif value is not None:
        if isinstance(value, bool):
            value = str(value).lower()
        result.append((prefix, value))
    return result


def _calendar_articles(session, path, past):
    url = urljoin(SOURCE_URL, path)
    if past:
        url += "?type=past"
    log_message("Fetching performance calendar", event="crawler_url_fetch", url=url)
    soup = BeautifulSoup(_get(session, url).text, "html.parser")
    articles = list(soup.select("article.event-item"))
    button = soup.select_one(".js-load-more[data-data]")
    if not button:
        return articles

    data = json.loads(button["data-data"])
    page_size = int(data.get("amount_to_load", 6))
    total = int(data.get("total_posts", len(articles)))
    seen_ids = {article.get("id") for article in articles}
    for loaded in range(page_size, total, page_size):
        data["post_count"] = loaded
        response = _get(session, LOAD_MORE_URL, params=_flatten(data))
        page_articles = BeautifulSoup(response.text, "html.parser").select(
            "article.event-item"
        )
        if not page_articles:
            break
        page_ids = {article.get("id") for article in page_articles}
        if page_ids <= seen_ids:
            break
        articles.extend(page_articles)
        seen_ids.update(page_ids)
    return articles


def _parse_dates(text):
    matches = list(MONTH_PATTERN.finditer(text))
    years = [(m.start(), int(m.group())) for m in re.finditer(r"\b20\d{2}\b", text)]
    parsed = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        days = [int(day) for day in re.findall(r"\b(?:[1-9]|[12]\d|3[01])\b", segment)]
        following = [year for position, year in years if match.start() < position]
        preceding = [year for position, year in years if position < match.start()]
        year = following[0] if following else (preceding[-1] if preceding else None)
        if year is None:
            continue
        for day in days:
            try:
                parsed.append(date(year, MONTHS[match.group().lower()], day).isoformat())
            except ValueError:
                continue
    return list(dict.fromkeys(parsed))


def _locations(text):
    lowered = text.casefold()
    found = []
    for needle, city, venue, country_code in LOCATION_RULES:
        if needle in lowered:
            item = (city, venue, country_code)
            if item not in found:
                found.append(item)
    return found


def _parse_article(article, past):
    title_node = article.select_one(".event-item-title")
    date_node = article.select_one(".event-item-date")
    location_node = article.select_one(".event-item-text-accompanying-artists")
    if not title_node or not date_node or not location_node:
        return []

    title = title_node.get_text(" ", strip=True)
    dates = _parse_dates(date_node.get_text(" ", strip=True))
    locations = _locations(location_node.get_text(" ", strip=True))
    event_url = article.get("data-href")
    if not event_url:
        link = article.select_one("a[href]")
        event_url = link.get("href") if link else None
    if not event_url:
        event_url = SOURCE_URL
    if not title or not dates or not locations:
        return []

    today = date.today().isoformat()
    dates = [value for value in dates if (value < today) == past]
    records = []
    # When multiple dates and locations are listed in parallel, pair them;
    # otherwise each advertised date belongs to the single listed venue.
    if len(locations) == len(dates):
        occurrences = zip(dates, locations)
    elif len(locations) == 1:
        occurrences = ((value, locations[0]) for value in dates)
    else:
        return []
    for value, (city, venue, country_code) in occurrences:
        records.append(
            {
                "title": title,
                "date": value,
                "url": urljoin(SOURCE_URL, event_url),
                "time_from": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": None,
            }
        )
    return records


class RolandoVillazonCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rolandovillazon_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "url"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/124.0 Safari/537.36"
                )
            }
        )
        records = []
        for path in FEEDS:
            for past in (False, True):
                for article in _calendar_articles(session, path, past):
                    parsed = _parse_article(article, past)
                    if not parsed:
                        log_message(
                            "Skipping incomplete or out-of-range performance",
                            event="crawler_record_skipped",
                            url=urljoin(SOURCE_URL, path),
                        )
                    records.extend(parsed)
        return records


def main():
    RolandoVillazonCrawler().run()


if __name__ == "__main__":
    main()
