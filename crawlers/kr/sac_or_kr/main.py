import calendar
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.sac.or.kr/site/main/home"
SOURCE = "Seoul Arts Center"
CALENDAR_URL = "https://www.sac.or.kr/site/main/program/getProgramCalList"
DETAIL_URL = "https://www.sac.or.kr/site/main/show/show_view?SN={sn}"
REQUEST_TIMEOUT = 30


def _clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip() or None


def _month_events(session, year, month, category):
    last_day = calendar.monthrange(year, month)[1]
    response = session.post(
        CALENDAR_URL,
        data={
            "searchYear": year,
            "searchMonth": month,
            "searchFirstDay": 1,
            "searchLastDay": last_day,
            "CATEGORY_PRIMARY": category,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("result") not in (None, "success"):
        raise ValueError(f"Unexpected calendar response for {year}-{month:02d}")

    events = []
    for day, items in payload.items():
        if not str(day).isdigit() or not isinstance(items, list):
            continue
        event_date = date(year, month, int(day)).isoformat()
        for item in items:
            item = dict(item)
            item["_date"] = event_date
            events.append(item)
    return events


def _description(url):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for panel in soup.select(".cwa-content.area > .cwa-tab-list > .ctl-sub"):
            heading = panel.select_one("h2.screenOut")
            if heading and "상세 정보" in heading.get_text(" ", strip=True):
                return _clean_text(panel.get_text("\n", strip=True))
    except requests.RequestException as error:
        log_message(
            "Failed to fetch concert detail",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
    return None


class SacOrKrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sac_or_kr",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="KR",
        upload_target="potential",
        dedupe_subset=["title", "date", "time_from", "venue", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"})

        # The calendar exposes past performances. Cover the complete current
        # year (including past months) and the next announced calendar year.
        current_year = date.today().year
        raw_events = []
        # 2 = Music Hall; 3 = Opera House. The latter is mixed, but is needed
        # to retain opera, ballet, musicals, and orchestral crossover events.
        for category in (2, 3):
            for year in (current_year, current_year + 1):
                for month in range(1, 13):
                    try:
                        raw_events.extend(_month_events(session, year, month, category))
                    except (requests.RequestException, ValueError) as error:
                        log_message(
                            "Failed to fetch calendar month",
                            event="crawler_url_fetch_failed",
                            url=CALENDAR_URL,
                            year=year,
                            month=month,
                            category=category,
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )

        records = []
        for item in raw_events:
            title = _clean_text(item.get("PROGRAM_SUBJECT"))
            venue = _clean_text(item.get("PLACE_NAME"))
            sn = item.get("SN")
            event_date = item.get("_date")
            if not all((title, venue, sn, event_date)):
                continue
            try:
                datetime.strptime(event_date, "%Y-%m-%d")
            except ValueError:
                continue
            records.append(
                {
                    "title": title,
                    "date": event_date,
                    "url": DETAIL_URL.format(sn=sn),
                    "time_from": _clean_text(item.get("PROGRAM_PLAYTIME")),
                    "time_to": None,
                    "venue": venue,
                    "city": "Seoul",
                    "description": None,
                }
            )

        descriptions = {}
        urls = sorted({record["url"] for record in records})
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(_description, url): url for url in urls}
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()
        for record in records:
            record["description"] = descriptions.get(record["url"])

        log_message(
            "SAC scrape completed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    SacOrKrCrawler().run()


if __name__ == "__main__":
    main()
