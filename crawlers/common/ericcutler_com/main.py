import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Eric Cutler"
SOURCE_URL = "https://ericcutler.com/"
CALENDAR_URL = f"{SOURCE_URL}category/calendar/"
AJAX_URL = f"{SOURCE_URL}wp-admin/admin-ajax.php"

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "SEPT": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}
MONTH_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# The calendar's location field alternates between a venue and a city. These
# first-party names are stable across the published archive.
LOCATION_MAP = {
    "Aix-en-Provence": ("Aix-en-Provence Festival", "Aix-en-Provence", "FR"),
    "Auditorium Grafenegg": ("Auditorium Grafenegg", "Grafenegg", "AT"),
    "Baden-Baden": ("Festspielhaus Baden-Baden", "Baden-Baden", "DE"),
    "Bayerische Staastoper": ("Bayerische Staatsoper", "Munich", "DE"),
    "Bayerische Staatsoper": ("Bayerische Staatsoper", "Munich", "DE"),
    "Bayreuther Festspiele": ("Bayreuther Festspiele", "Bayreuth", "DE"),
    "Berlin": ("Staatsoper Unter den Linden", "Berlin", "DE"),
    "Bordeaux": ("Opéra National de Bordeaux", "Bordeaux", "FR"),
    "Brussels": ("La Monnaie", "Brussels", "BE"),
    "Chicago": ("Lyric Opera of Chicago", "Chicago", "US"),
    "Cologne": ("Oper Köln", "Cologne", "DE"),
    "Deutsche Oper Berlin": ("Deutsche Oper Berlin", "Berlin", "DE"),
    "Dresden": ("Semperoper Dresden", "Dresden", "DE"),
    "Düsseldorf": ("Opernhaus Düsseldorf", "Düsseldorf", "DE"),
    "Dutch National Opera": ("Dutch National Opera", "Amsterdam", "NL"),
    "Frankfurt": ("Oper Frankfurt", "Frankfurt", "DE"),
    "Hamburg": ("Staatsoper Hamburg", "Hamburg", "DE"),
    "Houston, TX": ("Houston Grand Opera", "Houston", "US"),
    "Isarphilharmonie": ("Isarphilharmonie", "Munich", "DE"),
    "Köln": ("Oper Köln", "Cologne", "DE"),
    "London": ("Barbican Centre", "London", "GB"),
    "Lucerne": ("Lucerne Festival", "Lucerne", "CH"),
    "Madrid": ("Teatro Real", "Madrid", "ES"),
    "Mulhouse": ("La Filature", "Mulhouse", "FR"),
    "Naples": ("Teatro di San Carlo", "Naples", "IT"),
    "Opernhaus Zürich": ("Opernhaus Zürich", "Zürich", "CH"),
    "Paris": ("Philharmonie de Paris", "Paris", "FR"),
    "Pittsburgh, PA": ("Pittsburgh Symphony Orchestra", "Pittsburgh", "US"),
    "Royal Ballet & Opera": ("Royal Opera House", "London", "GB"),
    "Salzburger Festspiele": ("Salzburger Festspiele", "Salzburg", "AT"),
    "Salzburger Osterfestspiele": ("Salzburger Osterfestspiele", "Salzburg", "AT"),
    "Santa Fe, NM": ("Santa Fe Opera", "Santa Fe", "US"),
    "Semperoper Dresden": ("Semperoper Dresden", "Dresden", "DE"),
    "Staatsoper Berlin": ("Staatsoper Unter den Linden", "Berlin", "DE"),
    "Staatsoper Hamburg": ("Staatsoper Hamburg", "Hamburg", "DE"),
    "Strasbourg": ("Opéra national du Rhin", "Strasbourg", "FR"),
    "Stuttgart": ("Oper Stuttgart", "Stuttgart", "DE"),
    "The Metropolitan Opera": ("The Metropolitan Opera", "New York", "US"),
    "Theater an der Wien": ("Theater an der Wien", "Vienna", "AT"),
    "Vienna": ("Theater an der Wien", "Vienna", "AT"),
    "Wiener Staatsoper": ("Wiener Staatsoper", "Vienna", "AT"),
    "Zurich": ("Tonhalle Zürich", "Zürich", "CH"),
}

CITY_LOCATIONS = {
    "Aix-en-Provence", "Baden-Baden", "Berlin", "Bordeaux", "Brussels",
    "Chicago", "Cologne", "Dresden", "Düsseldorf", "Frankfurt", "Hamburg",
    "Houston, TX", "Köln", "London", "Lucerne", "Madrid", "Mulhouse",
    "Naples", "Paris", "Pittsburgh, PA", "Santa Fe, NM", "Strasbourg",
    "Stuttgart", "Vienna", "Zurich",
}


def parse_dates(value: str, starting_year: int) -> list[str]:
    """Expand strings such as 'DEC 31 JAN 1' into calendar dates."""
    value = re.sub(r"\b(?:19|20)\d{2}\b", "", value)
    matches = list(MONTH_PATTERN.finditer(value))
    parsed_dates = []
    year = starting_year
    previous_month = None

    for index, match in enumerate(matches):
        month = MONTHS[match.group(1).upper()]
        if previous_month is not None and month < previous_month:
            year += 1
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        day_text = value[match.end():end]
        for day_value in re.findall(r"\b\d{1,2}\b", day_text):
            parsed_dates.append(date(year, month, int(day_value)).isoformat())
        previous_month = month

    return parsed_dates


def calendar_pages(session: requests.Session, archive: bool):
    url = f"{CALENDAR_URL}?f=1" if archive else CALENDAR_URL
    log_message("Fetching calendar", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    yield soup

    button = soup.select_one(".ajax-load-more")
    if button is None:
        return

    page_number = int(button.get("data-pagenmbr", 1))
    post_year = button.get("data-postyear", "")
    while True:
        page_number += 1
        payload = {
            "action": button.get("data-function", "load_more_posts"),
            "page_nmbr": page_number,
            "include_cats": button.get("data-includecats", "3"),
            "template": button.get("data-template", "template-cal"),
            "post_status": button.get("data-poststatus", "publish" if archive else "future"),
            "post_year": post_year,
        }
        log_message(
            "Fetching calendar page",
            event="crawler_url_fetch",
            url=AJAX_URL,
            page_number=page_number,
        )
        response = session.post(AJAX_URL, data=payload, timeout=30)
        response.raise_for_status()
        if response.text.strip() == "0":
            return
        page_soup = BeautifulSoup(response.text, "html.parser")
        items = page_soup.select(".item[data-postyear]")
        if not items:
            return
        yield page_soup
        post_year = items[-1].get("data-postyear", post_year)


def parse_item(item) -> list[dict]:
    title_link = item.select_one(".title a[href]")
    dates_node = item.select_one(".dates")
    location_node = item.select_one(".location")
    if not title_link or not dates_node or not location_node:
        return []

    title = " ".join(title_link.stripped_strings)
    location = " ".join(location_node.stripped_strings)
    geography = LOCATION_MAP.get(location)
    if not title or not geography:
        log_message(
            "Skipping calendar entry with unresolved location",
            event="crawler_record_skipped",
            url=title_link.get("href", CALENDAR_URL),
            location=location,
        )
        return []

    venue, city, country_code = geography
    info_node = item.select_one(".info")
    info_lines = list(info_node.stripped_strings) if info_node else []
    if info_lines and info_lines[0] == location:
        info_lines = info_lines[1:]
    if location in CITY_LOCATIONS and info_lines:
        first_line = info_lines[0]
        if not re.search(r"\b(conductor|director|with)\b", first_line, re.IGNORECASE):
            venue = first_line
    description = "\n".join([title, *info_lines]) or None
    event_url = title_link["href"].strip()
    dates = parse_dates(dates_node.get_text(" ", strip=True), int(item["data-postyear"]))

    return [
        {
            "title": title,
            "date": event_date,
            "url": event_url,
            "time_from": None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": country_code,
            "description": description,
        }
        for event_date in dates
    ]


class EricCutlerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="ericcutler_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0; +https://github.com/)"
        )
        records = []
        for archive in (False, True):
            for page in calendar_pages(session, archive=archive):
                for item in page.select(".item[data-postyear]"):
                    records.extend(parse_item(item))
        log_message(
            "Calendar parsed",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    EricCutlerCrawler().run()


if __name__ == "__main__":
    main()
