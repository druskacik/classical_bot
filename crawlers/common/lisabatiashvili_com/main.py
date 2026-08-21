import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lisa Batiashvili"
SOURCE_URL = "https://lisabatiashvili.com/"
SCHEDULE_URLS = (
    "https://lisabatiashvili.com/schedule/",
    "https://lisabatiashvili.com/past-performances/",
)

# The site is a worldwide touring schedule.  Its location field contains a city
# (despite the CSS class being named ``country``), so countries are resolved
# from the finite set of cities used by the schedule and archive.
COUNTRIES_BY_CITY = {
    "AE": {"Abu Dhabi"},
    "AT": {"Bregenz", "Eisenstadt", "Grafenegg", "Innsbruck", "Salzburg", "Vienna"},
    "AU": {"Sydney"},
    "BE": {"Antwerp", "Brussels", "Ghent"},
    "CA": {"Montréal", "Toronto"},
    "CH": {"Bagnes", "Bürgenstock", "Lucerne", "Lugano", "Verbier", "Winterthur", "Zurich", "Zürich"},
    "CZ": {"Prague"},
    "DE": {"Audi Summer Concerts, Ingolstadt", "Bad Kissingen", "Baden-Baden", "Berlin", "Bonn", "Bremen", "Chemnitz", "Coesfeld", "Cologne", "Dortmund", "Dresden", "Düsseldorf", "Elmau", "Essen", "Frankfurt", "Freiburg", "Geisenheim-Johannisberg", "Hamburg", "Heidelberg", "Ingolstadt", "Kronberg im Taunus", "Krün", "Leipzig", "Ludwigsburg", "Ludwigshafen", "Ludwigshafen am Rhein", "Munich", "Neumarkt", "Oestrich", "Potsdam", "Rhein", "Wiesbaden"},
    "DK": {"Copenhagen"},
    "EE": {"Pärnu"},
    "ES": {"Barcelona", "Bilbao", "Castelló", "Las Palmas de Gran Canaria", "Madrid", "Santa Cruz de Tenerife", "Seville", "Tenerife", "Valencia", "Zaragoza"},
    "FI": {"Helsinki", "Turku"},
    "FR": {"Bordeaux", "Curtil-Vergy", "Lyon", "Paris", "Vougeot"},
    "GB": {"Aldeburgh", "Birmingham", "Edinburgh", "Glasgow", "London"},
    "GE": {"Tbilisi", "Tblisi", "Tsinandali"},
    "GR": {"Thessaloniki"},
    "HU": {"Budapest"},
    "IL": {"Haifa", "Tel Aviv"},
    "IT": {"Bologna", "Città della Pieve", "Cremona", "Meran", "Milan", "Pavia", "Rome", "Siena", "Torino", "Treviso", "Verona"},
    "JP": {"Kyoto", "Tokyo"},
    "KR": {"Seoul"},
    "LU": {"Luxembourg", "Luxembourg City"},
    "NL": {"Amsterdam", "Rotterdam"},
    "NO": {"Oslo", "Stavanger"},
    "PL": {"Wrocław"},
    "PT": {"Lisbon"},
    "SE": {"Stockholm"},
    "SI": {"Ljubljana"},
    "SK": {"Bratislava"},
    "TW": {"Taipei"},
}

CITY_TO_COUNTRY = {
    city: country_code
    for country_code, cities in COUNTRIES_BY_CITY.items()
    for city in cities
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _country_code(city: str) -> str | None:
    if re.search(r",\s*(?:CA|CO|D\.?\s*C\.?|FL|IA|IL|MA|MI|MN|MO|NJ|NY|PA)\.?$", city):
        return "US"
    return CITY_TO_COUNTRY.get(city)


def _dates(value: str) -> list[str]:
    match = re.fullmatch(r"([A-Z][a-z]{2})\s+([\d, ]+),\s+(\d{4})", _clean_text(value))
    if not match:
        return []
    month, days_text, year = match.groups()
    dates = []
    for day in re.findall(r"\d+", days_text):
        try:
            dates.append(datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date().isoformat())
        except ValueError:
            continue
    return dates


def _parse_page(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for article in soup.select("main article.show"):
        title_node = article.select_one("h3")
        date_node = article.select_one("time")
        city_node = article.select_one(".country")
        venue_node = article.select_one(".venue")
        link_node = article.select_one("a.hot-spot[href], a.btn[href]")
        if not all((title_node, date_node, city_node, venue_node, link_node)):
            continue

        title = _clean_text(title_node.get_text(" ", strip=True))
        city = _clean_text(city_node.get_text(" ", strip=True))
        venue = _clean_text(venue_node.get_text(" ", strip=True))
        url = link_node.get("href", "").strip()
        country_code = _country_code(city)
        dates = _dates(date_node.get_text(" ", strip=True))
        if not all((title, city, venue, url, country_code, dates)):
            log_message(
                "Skipping incomplete concert card",
                event="crawler_record_skipped",
                url=page_url,
                city=city or None,
                error_type="IncompleteRecord",
            )
            continue

        description_node = article.select_one(".performance-pieces")
        description = None
        if description_node:
            description = description_node.get_text("\n", strip=True)
            description = re.sub(r"\n{3,}", "\n\n", description).strip() or None

        for concert_date in dates:
            records.append(
                {
                    "title": title,
                    "date": concert_date,
                    "url": url,
                    "time_from": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )
    return records


class LisaBatiashviliCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lisabatiashvili_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "url"],
    )

    def scrape(self) -> list[dict]:
        records = []
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})
        for page_url in SCHEDULE_URLS:
            log_message("Fetching schedule page", event="crawler_url_fetch", url=page_url)
            response = session.get(page_url, timeout=30)
            response.raise_for_status()
            page_records = _parse_page(response.text, page_url)
            log_message(
                "Schedule page parsed",
                event="crawler_page_parsed",
                url=page_url,
                record_count=len(page_records),
            )
            records.extend(page_records)
        return records


def main():
    LisaBatiashviliCrawler().run()


if __name__ == "__main__":
    main()
