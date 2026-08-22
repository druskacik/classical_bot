import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://regulamuehlemann.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "calendar/")
SOURCE = "Regula Mühlemann"
MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _city_key(value):
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


COUNTRIES = {
    _city_key(city): code
    for code, cities in {
        "AT": ["Bregenz", "Gmunden", "Grafenegg", "Kufstein", "Linz", "Salzburg", "Schwarzenberg", "Vienna"],
        "BE": ["Antwerp", "Bruges", "Brussels"],
        "CH": ["Aarau", "Basel", "Beinwil", "Bellmund", "Bern", "Biel", "Boswil", "Brugg", "Cham", "Düdingen", "Ermatingen", "Frauenfeld", "Fribourg", "Geneva", "Gstaad", "La Chaux-de-Fonds", "Lausanne", "Liestal", "Locarno", "Lucerne", "Luzern", "Olten", "Rheinfelden", "Riehen", "Rigi Kulm", "Saanen", "Schaffhausen", "Sils im Engadin", "Solothurn", "St. Gallen", "Sursee", "Thun", "Vevey", "Vitznau", "Warth-Weiningen", "Winterthur", "Zug", "Zurich", "Zürich"],
        "DE": ["Aschaffenburg", "Bad Lauchstädt", "Bad Wörishofen", "Baden-Baden", "Bamberg", "Bayreuth", "Berlin", "Blaibach", "Breisgau", "Bremen", "Cologne", "Dortmund", "Dresden", "Düsseldorf", "Essen", "Frankfurt", "Freiburg", "Freyung", "Friedrichshafen", "Fulda", "Halle", "Hamburg", "Kempen", "Kempten", "Köln", "Künzelsau", "Leipzig", "Lindau", "Lörrach", "Ludwigsburg", "Munich", "Müllheim", "Nienburg", "Oettingen", "Straubing", "Stuttgart", "Villingen-Schwenningen", "Weiden in der Oberpfalz", "Weimar", "Weingarten", "Wildeshausen", "Wismar", "Würzburg"],
        "ES": ["Barcelona", "Madrid", "Murcia"],
        "FR": ["Dijon", "Grenoble", "Marseille", "Paris", "Poitiers"],
        "GB": ["Birmingham", "London"], "GR": ["Epidaurus"], "HU": ["Budapest"],
        "IE": ["Dublin"],
        "IT": ["Bozen", "Brescia", "Florence", "Meran", "Milan", "Naples", "Pisa", "Pordenone", "Rome", "Toblach", "Torino", "Venice", "Verona", "Vicenza"],
        "LB": ["Beirut"], "LI": ["Liechtenstein", "Schaan"], "LV": ["Liepāja"],
        "LU": ["Luxembourg", "Luxembourg City"], "MC": ["Monaco", "Monte-Carlo"],
        "NL": ["Amsterdam"], "NO": ["Stavanger"],
        "PL": ["Kraków", "Poznań", "Wrocław"], "RO": ["Bucharest"],
        "SE": ["Stockholm", "Uppsala"],
        "US": ["Chapel Hill", "Chicago", "Fort Lauderdale", "Irvine", "Los Angeles", "New York, NY", "Norfolk", "Philadelphia, PA"],
    }.items()
    for city in cities
}


def _text(item, selector):
    node = item.select_one(selector)
    return node.get_text(" ", strip=True) if node else ""


def _days(value):
    return [int(day) for day in re.findall(r"\d+", value)]


def _parse_page(html, *, past, today):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".calendar-item")
    records = []
    year = today.year
    previous_month = None

    for item in items:
        month = MONTHS.get(_text(item, ".calendar-dates-months").upper()[:3])
        days = _days(_text(item, ".calendar-dates-days"))
        if not month or not days:
            continue

        if previous_month is None:
            edge = date(year, month, max(days) if past else min(days))
            if past and edge >= today:
                year -= 1
            elif not past and date(year, month, max(days)) < today:
                year += 1
        elif past and month > previous_month:
            year -= 1
        elif not past and month < previous_month:
            year += 1
        previous_month = month

        city = _text(item, ".calendar-location")
        venue = _text(item, ".calendar-venue")
        # One archived entry has these two fields reversed on the source page.
        if city == "Festsaal im Residenzschloss" and venue == "Oettingen":
            city, venue = venue, city
        country_code = COUNTRIES.get(_city_key(city))
        title = _text(item, ".calendar-title")
        if not (title and city and venue and country_code):
            log_message(
                "Skipping incomplete calendar item",
                event="crawler_item_skipped",
                city=city or None,
                venue=venue or None,
                has_title=bool(title),
            )
            continue

        notes = item.select_one(".calendar-notes")
        description = notes.get_text("\n", strip=True) if notes else None
        href = item.get("href")
        show_id = item.get("data-show-id")
        url = urljoin(CALENDAR_URL, href) if href else f"{CALENDAR_URL}?type=past#show-{show_id}"
        for day in days:
            try:
                event_date = date(year, month, day).isoformat()
            except ValueError:
                continue
            records.append({
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": None,
                "venue": venue,
                "city": city,
                "country_code": country_code,
                "description": description,
            })
    return records


class RegulaMuehlemannCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="regulamuehlemann_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self):
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0; +https://regulamuehlemann.com/)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        today = date.today()
        records = []
        for url, past in ((CALENDAR_URL, False), (f"{CALENDAR_URL}?type=past", True)):
            log_message("Fetching calendar", event="crawler_url_fetch", url=url)
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            records.extend(_parse_page(response.text, past=past, today=today))
        return records


def main():
    RegulaMuehlemannCrawler().run()


if __name__ == "__main__":
    main()
