import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.hkphil.org/"
SOURCE = "Hong Kong Philharmonic Orchestra"
CALENDAR_URL = urljoin(SOURCE_URL, "concert")
TIMEOUT = 40
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)"}
ALLOWED_TYPES = {
    "Orchestra Concert",
    "Community Concert",
    "Chamber Music Concert",
    "Tour",
    "Collaboration",
}
DATE_RE = re.compile(
    r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})\s+\([^)]+\)\s+"
    r"(\d{1,2}:\d{2}\s*(?:am|pm))",
    re.IGNORECASE,
)


# Tour cities found in HK Phil's first-party archive. Venues not matching one
# of these are Hong Kong venues; the organisation's calendar is based there.
CITY_COUNTRIES = {
    "Amsterdam": "NL", "Barcelona": "ES", "Beijing": "CN",
    "Berlin": "DE", "Birmingham": "GB", "Brussels": "BE",
    "Changsha": "CN", "Chengdu": "CN", "Chuncheon": "KR",
    "Daegu": "KR", "Daejeon": "KR", "Dortmund": "DE",
    "Dongguan": "CN", "Dresden": "DE", "Eindhoven": "NL",
    "Foshan": "CN", "Grafenegg": "AT", "Guangzhou": "CN",
    "Gwangju": "KR", "Hamburg": "DE", "Hannover": "DE",
    "Harbin": "CN", "Helsinki": "FI", "Hyogo": "JP",
    "Kawasaki": "JP", "London": "GB", "Macao": "MO",
    "Melbourne": "AU", "Merano": "IT", "Munich": "DE",
    "München": "DE", "Rome": "IT", "Seoul": "KR",
    "Shanghai": "CN", "Shenyang": "CN", "Shenzhen": "CN",
    "Singapore": "SG", "Sinagpore": "SG", "Sydney": "AU",
    "Taipei": "TW", "Tianjin": "CN", "Tokyo": "JP",
    "Toulouse": "FR", "Vienna": "AT", "Wien": "AT",
    "Wuhan": "CN", "Wuxi": "CN", "Xiamen": "CN",
    "Zürich": "CH", "Zhuhai": "CN",
}


def _clean_text(element):
    if element is None:
        return None
    text = "\n".join(
        line.strip() for line in element.get_text("\n", strip=True).splitlines()
        if line.strip()
    )
    return text or None


def _location(venue):
    for city, country_code in CITY_COUNTRIES.items():
        if re.search(rf"\b{re.escape(city)}\b", venue, re.IGNORECASE):
            return city.replace("Sinagpore", "Singapore").replace("München", "Munich").replace("Wien", "Vienna"), country_code

    if "Thailand" in venue or "Mahidol University" in venue:
        return "Bangkok", "TH"
    if "Suntory Hall" in venue:
        return "Tokyo", "JP"
    if "Kursaal" in venue:
        return "Merano", "IT"
    if "Wolkenturm" in venue:
        return "Grafenegg", "AT"
    if "BOZAR" in venue:
        return "Brussels", "BE"
    return "Hong Kong", "HK"


def _get_soup(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _season_urls():
    soup = _get_soup(CALENDAR_URL)
    options = soup.select('select[name="season"] option[value]')
    values = [option["value"] for option in options if option["value"]]
    if not values:
        raise ValueError("No seasons found on the concert calendar")
    return [urljoin(CALENDAR_URL + "/", value) for value in values]


def _detail_urls():
    urls = set()
    for season_url in _season_urls():
        log_message("Fetching concert season", event="crawler_url_fetch", url=season_url)
        soup = _get_soup(season_url)
        for card in soup.select(".new-hl-event"):
            event_type = _clean_text(card.select_one(".new-hl-event__type"))
            if event_type not in ALLOWED_TYPES:
                continue
            link = card.select_one('a.new-hl-event__title[href^="/concert/"]')
            if link:
                urls.add(urljoin(SOURCE_URL, link["href"]))
    return sorted(urls)


def _parse_detail(url):
    soup = _get_soup(url)
    title = _clean_text(soup.select_one(".info-box h1")) or _clean_text(soup.select_one("h1"))
    venue_box = soup.select_one(".info-box__venue")
    venue_parts = venue_box.select("p") if venue_box else []
    venue = _clean_text(venue_parts[-1]) if len(venue_parts) >= 2 else None
    date_text = _clean_text(soup.select_one(".info-box__item--1")) or ""
    description = _clean_text(soup.select_one(".inner-content--concert .content-box"))

    if not title or not venue:
        log_message("Skipping concert with missing required detail", event="crawler_record_skipped", url=url)
        return []

    city, country_code = _location(venue)
    records = []
    for date_value, time_value in DATE_RE.findall(date_text):
        try:
            concert_date = datetime.strptime(date_value.upper(), "%d %b %Y").date().isoformat()
            time_from = datetime.strptime(time_value.replace(" ", "").upper(), "%I:%M%p").time().isoformat()
        except ValueError:
            continue
        records.append({
            "title": title,
            "date": concert_date,
            "url": url,
            "time_from": time_from,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        })
    if not records:
        log_message("Skipping concert with no parseable occurrence", event="crawler_record_skipped", url=url)
    return records


class HKPhilCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="hkphil_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HK",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        urls = _detail_urls()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_parse_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Concert detail request failed",
                        event="crawler_url_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record["date"], record["time_from"], record["title"]))
        return records


def main():
    HKPhilCrawler().run()


if __name__ == "__main__":
    main()
