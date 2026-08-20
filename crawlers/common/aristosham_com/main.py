import calendar
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.aristosham.com/"
SOURCE = "Aristo Sham"
SCHEDULE_URLS = (
    urljoin(SOURCE_URL, "schedule"),
    urljoin(SOURCE_URL, "previous-engagements"),
)

COUNTRY_CODES = {
    "Argentina": "AR",
    "Brazil": "BR",
    "China": "CN",
    "Denmark": "DK",
    "Finland": "FI",
    "France": "FR",
    "Georgia": "GE",
    "Germany": "DE",
    "Hong Kong": "HK",
    "Italy": "IT",
    "Japan": "JP",
    "Korea": "KR",
    "Netherlands": "NL",
    "Oman": "OM",
    "Peru": "PE",
    "Philippines": "PH",
    "Poland": "PL",
    "Singapore": "SG",
    "South Korea": "KR",
    "Spain": "ES",
    "Sweden": "SE",
    "Taiwan": "TW",
    "USA": "US",
}


def _clean_text(value):
    if not value:
        return ""
    value = value.replace("\u200d", "")
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()


def _parse_location(value):
    parts = [_clean_text(part) for part in value.split(",") if _clean_text(part)]
    if not parts:
        return None

    country_name = parts[-1]
    if len(parts) == 1 and country_name in {"Hong Kong", "Singapore"}:
        city = country_name
    else:
        city = parts[0]

    # Webflow records sometimes render Hong Kong as a city followed by China;
    # use its own ISO 3166-1 code consistently with the rest of the schedule.
    country_code = "HK" if parts[0] == "Hong Kong" else COUNTRY_CODES.get(country_name)
    if not city or not country_code:
        return None
    return city, country_code


def _month_number(value):
    value = value.strip().capitalize().replace("Janurary", "January")
    try:
        return list(calendar.month_name).index(value)
    except ValueError as error:
        raise ValueError(f"Unknown month name: {value}") from error


def _parse_dates(value):
    """Expand the site's single dates, day lists, and inclusive date ranges."""
    value = _clean_text(value).replace("–", "-").replace("—", "-")
    value = value.replace("Janurary", "January")

    single = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", value)
    if single:
        month, day, year = single.groups()
        return [datetime(int(year), _month_number(month), int(day)).date().isoformat()]

    day_list = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})\s*&\s*(\d{1,2}),\s*(\d{4})", value
    )
    if day_list:
        month, first_day, second_day, year = day_list.groups()
        return [
            datetime(int(year), _month_number(month), int(day)).date().isoformat()
            for day in (first_day, second_day)
        ]

    same_month_range = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),\s*(\d{4})", value
    )
    if same_month_range:
        month, first_day, last_day, year = same_month_range.groups()
        month_number = _month_number(month)
        return [
            datetime(int(year), month_number, day).date().isoformat()
            for day in range(int(first_day), int(last_day) + 1)
        ]

    cross_month_range = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})",
        value,
    )
    if cross_month_range:
        first_month, first_day, last_month, last_day, year = cross_month_range.groups()
        start = datetime(int(year), _month_number(first_month), int(first_day)).date()
        end = datetime(int(year), _month_number(last_month), int(last_day)).date()
        return [
            datetime.fromordinal(ordinal).date().isoformat()
            for ordinal in range(start.toordinal(), end.toordinal() + 1)
        ]

    raise ValueError(f"Unsupported date format: {value}")


def _parse_time(value):
    value = _clean_text(value).upper().replace(" ", "")
    if not value:
        return None
    for pattern in ("%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(value, pattern).strftime("%H:%M:%S")
        except ValueError:
            pass
    return None


class AristoShamCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="aristosham_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        records = []
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ClassicalBot/1.0)"

        for page_url in SCHEDULE_URLS:
            log_message("Fetching schedule page", event="crawler_url_fetch", url=page_url)
            try:
                response = session.get(page_url, timeout=30)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    "Schedule fetch failed",
                    event="crawler_url_fetch_failed",
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, "html.parser")
            for item in soup.select(".schedule-text7_item"):
                columns = item.select(".schedule-text7_col")
                center = item.select_one(".schedule-text7_col.is-center")
                if not columns or center is None:
                    continue

                subtitles = columns[0].select(".subtitle")
                raw_date = _clean_text(subtitles[0].get_text(" ")) if subtitles else ""
                raw_time = _clean_text(subtitles[1].get_text(" ")) if len(subtitles) > 1 else ""
                location_node = center.select_one("p")
                venue_node = center.select_one("h5")
                title_node = center.select_one("h4")
                link = item.select_one("a.viewport-absolute")

                location = _parse_location(location_node.get_text(" ") if location_node else "")
                venue = _clean_text(venue_node.get_text(" ") if venue_node else "")
                title = _clean_text(title_node.get_text(" ") if title_node else "")
                if not raw_date or not location or not venue or not title:
                    continue

                href = link.get("href", "") if link else ""
                event_url = urljoin(page_url, href) if href and href != "#" else page_url
                description_node = center.select_one(".w-richtext")
                description = None
                if description_node:
                    description = _clean_text(description_node.get_text("\n")) or None

                try:
                    dates = _parse_dates(raw_date)
                except ValueError as error:
                    log_message(
                        "Skipping event with unsupported date",
                        event="crawler_event_skipped",
                        url=event_url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue

                city, country_code = location
                for date_value in dates:
                    records.append(
                        {
                            "title": title,
                            "date": date_value,
                            "url": event_url,
                            "time_from": _parse_time(raw_time),
                            "venue": venue,
                            "city": city,
                            "country_code": country_code,
                            "description": description,
                        }
                    )

        return records


def main():
    AristoShamCrawler().run()


if __name__ == "__main__":
    main()
