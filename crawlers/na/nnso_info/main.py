import re
from datetime import date, datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Namibian National Symphony Orchestra"
SOURCE_URL = "https://www.nnso.info/"
CALENDAR_URL = urljoin(SOURCE_URL, "content.aspx")
CLUB_ID = "19472"
PERFORMANCE_CATEGORIES = {
    "Baroque Festival",
    "Concerto Festival",
    "Other concerts",
}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


class NnsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nnso_info",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NA",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date"],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _get(self, url, *, params=None):
        log_message("Fetching NNSO page", event="crawler_url_fetch", url=url)
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response

    def _start_session(self):
        # ClubExpress's WAF establishes the cookies required by content.aspx on
        # the home page. Direct calendar requests otherwise return an empty 404.
        self._get(SOURCE_URL)

    @staticmethod
    def _event_links(soup):
        links = set()
        for anchor in soup.select('a[href*="page_id=4002"][href*="event_date_id="]'):
            links.add(urljoin(SOURCE_URL, anchor.get("href")))
        return links

    @staticmethod
    def _month_event_dates(soup, year, month):
        dates = {}
        for container in soup.select(".list-event-container"):
            anchor = container.select_one('a[href*="page_id=4002"][href*="event_date_id="]')
            if not anchor:
                continue
            text = list(container.stripped_strings)
            day = next((value for value in text[:3] if value.isdigit()), None)
            if not day:
                continue
            weekday = datetime(year, month, int(day)).strftime("%A")
            raw_date = f"{weekday}, {datetime(year, month, int(day)).strftime('%B')} {day}, {year}"
            detail_text = " ".join(container.select_one(".event-details-text").stripped_strings)
            time_match = re.search(r"(\d{1,2}:\d{2} [AP]M)(?: until (\d{1,2}:\d{2} [AP]M))?", detail_text)
            if time_match:
                raw_date += f", {time_match.group(1)}"
                if time_match.group(2):
                    raw_date += f" until {time_match.group(2)}"
            dates[urljoin(SOURCE_URL, anchor.get("href"))] = raw_date
        return dates

    def _calendar_page(self, **params):
        full_params = {"page_id": "4001", "club_id": CLUB_ID, **params}
        return BeautifulSoup(self._get(CALENDAR_URL, params=full_params).content, "html.parser")

    def _all_event_links(self):
        # The Future view is an unpaginated list of every forthcoming event.
        future = self._calendar_page(action="cira", vm="Future", sif="0")
        links = self._event_links(future)
        self._calendar_dates = {}

        # ClubExpress has no Past list. Walk its stable dated MonthView backwards
        # until two completely empty calendar years precede the oldest event.
        today = date.today()
        year, month = today.year, today.month
        empty_months = 0
        found_past = False
        while not found_past or empty_months < 24:
            month_page = self._calendar_page(
                action="cira",
                vm="MonthView",
                sif="0",
                vd=f"{month}/1/{year}",
            )
            month_links = self._event_links(month_page)
            links.update(month_links)
            self._calendar_dates.update(self._month_event_dates(month_page, year, month))
            if month_links:
                found_past = True
                empty_months = 0
            else:
                empty_months += 1

            month -= 1
            if month == 0:
                month = 12
                year -= 1

        return sorted(links)

    @staticmethod
    def _section(soup, heading):
        header = next(
            (node for node in soup.find_all("h3") if node.get_text(" ", strip=True) == heading),
            None,
        )
        return header.parent if header else None

    @staticmethod
    def _clean_text(node):
        if node is None:
            return None
        text = node.get_text("\n", strip=True)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or None

    def _parse_event(self, url):
        soup = BeautifulSoup(self._get(url).content, "html.parser")

        category_section = self._section(soup, "Category")
        category_row = category_section.select_one(".form-row") if category_section else None
        category = self._clean_text(category_row)
        if category not in PERFORMANCE_CATEGORIES:
            return None

        about = self._section(soup, "About this event")
        title_node = (about.find("h2") if about else None) or soup.find("h2")
        title = self._clean_text(title_node)

        date_section = self._section(soup, "Date")
        date_row = date_section.select_one(".date-row > div") if date_section else None
        raw_date = self._clean_text(date_row)
        return self._parse_event_soup(soup, url, raw_date)

    @staticmethod
    def _parse_date_time(raw_date):
        match = re.fullmatch(
            r"[A-Za-z]+, ([A-Za-z]+ \d{1,2}, \d{4})"
            r"(?:, (\d{1,2}:\d{2} [AP]M)(?: until (\d{1,2}:\d{2} [AP]M))?)?",
            raw_date or "",
        )
        if not match:
            raise ValueError(f"Unrecognized event date: {raw_date!r}")
        event_date = datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()

        def parse_time(value):
            return datetime.strptime(value, "%I:%M %p").time().isoformat() if value else None

        return event_date, parse_time(match.group(2)), parse_time(match.group(3))

    def _parse_event_soup(self, soup, url, raw_date):
        category_section = self._section(soup, "Category")
        category_row = category_section.select_one(".form-row") if category_section else None
        category = self._clean_text(category_row)
        if category not in PERFORMANCE_CATEGORIES:
            return None

        about = self._section(soup, "About this event")
        title_node = (about.find("h2") if about else None) or soup.find("h2")
        title = self._clean_text(title_node)

        try:
            event_date, time_from, time_to = self._parse_date_time(raw_date)
        except (TypeError, ValueError):
            return None

        location_section = self._section(soup, "Location")
        location = location_section.select_one(".event-location-text, .form-row") if location_section else None
        if location is None:
            return None
        location_lines = [
            line.strip()
            for line in location.get_text("\n", strip=True).splitlines()
            if line.strip()
        ]
        venue = location_lines[0] if location_lines else None

        city = None
        map_link = location_section.select_one('.map-link a[href*="maps?q="]')
        if map_link:
            query = parse_qs(urlparse(map_link.get("href")).query).get("q", [""])[0]
            parts = [part.strip() for part in query.split(",") if part.strip()]
            if parts and parts[-1] == "NAM":
                parts.pop()
            if parts:
                city = parts[-1] or None
        if not city and len(location_lines) >= 2:
            candidates = [line for line in location_lines[1:] if line != "NAM" and not line.startswith("http")]
            city = candidates[-1] if candidates else None
        if not title or not venue or not city:
            return None

        description_node = about.find("div", recursive=False) if about else None
        return {
            "title": title,
            "date": event_date,
            "url": url,
            "time_from": time_from,
            "time_to": time_to,
            "venue": venue,
            "city": city,
            "description": self._clean_text(description_node),
        }

    def _occurrence_dates(self, urls):
        """Map ClubExpress occurrence URLs to their displayed date/time."""
        grouped = {}
        for url in urls:
            query = parse_qs(urlparse(url).query)
            grouped.setdefault(query["item_id"][0], []).append(url)

        result = dict(self._calendar_dates)
        for item_id, item_urls in grouped.items():
            if all(url in result for url in item_urls):
                continue
            item_urls.sort(key=lambda value: int(parse_qs(urlparse(value).query)["event_date_id"][0]))
            soup = BeautifulSoup(self._get(item_urls[0]).content, "html.parser")
            date_section = self._section(soup, "Date")
            date_row = date_section.select_one(".date-row > div") if date_section else None
            dates = [self._clean_text(date_row)]
            if len(item_urls) > 1:
                repeated_url = urljoin(SOURCE_URL, "handlers/repeating_event_dates.ashx")
                response = self._get(repeated_url, params={"event_id": item_id, "club_id": CLUB_ID})
                repeated = BeautifulSoup(response.content, "html.parser").get_text("\n", strip=True)
                dates.extend(line.strip() for line in repeated.splitlines() if line.strip())
            if len(dates) != len(item_urls):
                log_message(
                    "NNSO occurrence dates did not match event links",
                    event="crawler_parse_warning",
                    url=item_urls[0],
                    link_count=len(item_urls),
                    date_count=len(dates),
                )
                continue
            result.update(zip(item_urls, dates))
        return result

    def scrape(self):
        self._start_session()
        records = []
        urls = self._all_event_links()
        occurrence_dates = self._occurrence_dates(urls)
        for url in urls:
            try:
                raw_date = occurrence_dates.get(url)
                if not raw_date:
                    continue
                soup = BeautifulSoup(self._get(url).content, "html.parser")
                record = self._parse_event_soup(soup, url, raw_date)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch NNSO event",
                    event="crawler_url_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        log_message(
            "NNSO scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    NnsoCrawler().run()


if __name__ == "__main__":
    main()
