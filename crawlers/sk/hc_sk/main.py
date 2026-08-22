import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://hc.sk/"
CALENDAR_URL = urljoin(SOURCE_URL, "kalendarium")
SOURCE = "Hudobné centrum"
ARCHIVE_START = (2022, 1)

# The calendar is a mixed, user-submitted music calendar.  These first-party
# genres cover classical music and adjacent categories which can contain opera,
# operetta, sacred music, art music, film music, crossover, and musicals.  The
# resulting candidates therefore go through the potential-event classifier.
GENRE_IDS = ("10", "11", "13", "17", "21", "70", "80", "85", "90", "96")
REQUEST_TIMEOUT = 30
MAX_WORKERS = 8


def _month_sequence(start, end):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def _add_months(year, month, count):
    index = year * 12 + month - 1 + count
    return index // 12, index % 12 + 1


def _clean_text(element):
    if element is None:
        return None
    text = "\n".join(element.stripped_strings)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


class HcCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="hc_sk",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="SK",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self, start_month=None, end_month=None):
        today = date.today()
        self.start_month = start_month or ARCHIVE_START
        self.end_month = end_month or _add_months(today.year, today.month, 18)
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "classical-concert-crawler/1.0 (+https://hc.sk/)"}
        )

    def _get_soup(self, url, params=None):
        log_message("Fetching calendar page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def _calendar_params(self, year, month, genre):
        return {
            "kalendarium-location": "-1",
            "kalendarium-okres": "-1",
            "kalendarium-city": "-1",
            "kalendarium-genre": genre,
            "kalendarium-name": "",
            "kalendarium-type": "0",
            "kalendarium-day": "",
            "kalendarium-month": f"{month:02d}",
            "kalendarium-year": f"{year % 100:02d}",
            "do": "kalendarium-date",
        }

    def _discover_days(self, year, month, genre):
        soup = self._get_soup(CALENDAR_URL, self._calendar_params(year, month, genre))
        days = set()
        for link in soup.select('a[href*="kalendarium-day="]'):
            href = urljoin(CALENDAR_URL, link.get("href", ""))
            query = parse_qs(urlparse(href).query)
            try:
                link_year = 2000 + int(query["kalendarium-year"][0])
                link_month = int(query["kalendarium-month"][0])
                link_day = int(query["kalendarium-day"][0])
                date(link_year, link_month, link_day)
            except (KeyError, ValueError):
                continue
            # Calendar grids include days from the adjoining months.
            if (link_year, link_month) == (year, month):
                days.add((link_year, link_month, link_day))
        return days

    def _discover_event_urls(self, event_day):
        year, month, day = event_day
        params = self._calendar_params(year, month, "")
        params["kalendarium-day"] = f"{day:02d}"
        soup = self._get_soup(CALENDAR_URL, params)
        return {
            urljoin(SOURCE_URL, link["href"])
            for link in soup.select('a.kal-box[href*="/kalendarium/detail/"]')
            if link.get("href")
        }

    @staticmethod
    def _metadata(soup):
        values = {}
        for title in soup.select(".info-wrap .info-title"):
            value = title.find_next_sibling(class_="info")
            if value:
                values[title.get_text(" ", strip=True).rstrip(":").casefold()] = _clean_text(value)
        return values

    def _parse_detail(self, url):
        soup = self._get_soup(url)
        title = _clean_text(soup.select_one(".kal-event-name h1"))
        date_wrappers = soup.select(".date .multi-wrapper")
        # A range or overview is not a concrete occurrence. Individual
        # occurrences on this site have exactly one three-part day wrapper.
        if not title or len(date_wrappers) != 1:
            return None
        date_parts = [part.get_text(" ", strip=True).rstrip(".") for part in date_wrappers[0].select(".day-wrap span")]
        if len(date_parts) != 3:
            return None
        try:
            event_date = date(int(date_parts[2]), int(date_parts[1]), int(date_parts[0])).isoformat()
        except ValueError:
            return None

        metadata = self._metadata(soup)
        genre = metadata.get("žáner", "").casefold()
        accepted_genres = {
            "vážna hudba", "scénická a filmová hudba", "muzikál",
            "experimentálna hudba", "dychová hudba", "gospel", "a cappella",
            "chrámová hudba", "viacžánrové", "opereta",
        }
        if not any(item.strip() in accepted_genres for item in genre.split(",")):
            return None

        place = metadata.get("miesto")
        venue = metadata.get("sála")
        if not place or not venue:
            return None
        venue = re.sub(r"\nMapa$", "", venue, flags=re.IGNORECASE).strip()
        city = place.split(",", 1)[0].strip()
        if " - " in city:
            city = city.split(" - ", 1)[0].strip()
        if not city or city.casefold() == venue.casefold():
            return None

        time_text = _clean_text(date_wrappers[0].select_one(".time")) or ""
        times = re.findall(r"(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?!\d)", time_text)
        description = _clean_text(soup.select_one(".page-content .container.d-block"))
        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": times[0] if times else None,
            "time_to": times[1] if len(times) > 1 else None,
            "venue": venue,
            "city": city,
            "country_code": "SK",
            "description": description,
        }

    def scrape(self):
        days = set()
        for year, month in _month_sequence(self.start_month, self.end_month):
            for genre in GENRE_IDS:
                try:
                    days.update(self._discover_days(year, month, genre))
                except requests.RequestException as error:
                    log_message(
                        "Calendar month fetch failed",
                        event="crawler_fetch_failed",
                        url=CALENDAR_URL,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        event_urls = set()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(self._discover_event_urls, item): item for item in days}
            for future in as_completed(futures):
                try:
                    event_urls.update(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Calendar day fetch failed",
                        event="crawler_fetch_failed",
                        url=CALENDAR_URL + "?" + urlencode({"day": futures[future]}),
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(self._parse_detail, url): url for url in event_urls}
            for future in as_completed(futures):
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        "Event detail fetch failed",
                        event="crawler_fetch_failed",
                        url=futures[future],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        log_message(
            "Calendar scrape parsed",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    HcCrawler().run()


if __name__ == "__main__":
    main()
