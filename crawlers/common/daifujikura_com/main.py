import calendar
import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://daifujikura.com/"
CALENDAR_URL = urljoin(SOURCE_URL, "concerts")
SOURCE = "Dai Fujikura"

COUNTRIES = {
    "australia": "AU", "austria": "AT", "belgium": "BE", "brazil": "BR",
    "canada": "CA", "china": "CN", "croatia": "HR", "czech republic": "CZ",
    "denmark": "DK", "england": "GB", "estonia": "EE", "finland": "FI",
    "france": "FR", "germany": "DE", "greece": "GR", "hong kong": "HK",
    "hungary": "HU", "iceland": "IS", "ireland": "IE", "israel": "IL",
    "italy": "IT", "japan": "JP", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "mexico": "MX", "netherlands": "NL", "holland": "NL",
    "new zealand": "NZ", "norway": "NO", "poland": "PL", "portugal": "PT",
    "romania": "RO", "scotland": "GB", "serbia": "RS", "singapore": "SG",
    "slovakia": "SK", "slovenia": "SI", "south korea": "KR", "korea": "KR",
    "spain": "ES", "sweden": "SE", "switzerland": "CH", "taiwan": "TW",
    "the netherlands": "NL", "uk": "GB", "u.k.": "GB",
    "united kingdom": "GB", "usa": "US", "u.s.a.": "US",
    "united states": "US", "wales": "GB",
}

US_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "dc",
}

MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
MONTH_PATTERN = "|".join(calendar.month_name[1:])
DATE_LINE = re.compile(
    rf"^(?:(?:{MONTH_PATTERN})\s+\d{{1,2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_PATTERN}))",
    re.I,
)
DATE_VALUE = re.compile(
    rf"(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"|(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month2>{MONTH_PATTERN})",
    re.I,
)
TIME_VALUE = re.compile(r"(?<!\d)(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)(?!\w)|(?<!\d)(\d{1,2}):(\d{2})(?!\d)", re.I)
VENUE_WORDS = re.compile(
    r"\b(?:hall|theat(?:re|er)|opera|museum|museo|church|chapel|cathedral|abbey|"
    r"conservator|university|college|center|centre|auditorium|academy|studio|barbican|"
    r"concertgebouw|philharmonie|laeiszhalle|suntory|venue|temple|synagogue)\b",
    re.I,
)
CITY_HINTS = (
    "New Taipei City", "Tokyo", "New York", "London", "Boston", "Glasgow",
    "Aberdeen", "Bochum", "Wrocław", "Portland", "Benedict", "Vigo", "Kraków",
    "Taipei", "Milan", "Aichi", "Kawasaki", "Basel", "Berlin", "Daegu",
    "Hamburg", "Amsterdam", "Cambridge", "Aarhus", "Vienna", "Nantucket",
    "Hitzacker", "Osaka", "Brussels", "Edinburgh", "Hong Kong", "Haarlem",
    "Nagoya",
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip(" ,:;|.-")


def _country(text):
    lowered = text.casefold()
    for name in sorted(COUNTRIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered):
            return COUNTRIES[name], name
    return None, None


def _dates(line):
    year_match = re.search(r"\b((?:19|20)\d{2})\b", line)
    if not year_match:
        return []
    year = int(year_match.group(1))
    match = DATE_VALUE.search(line)
    if not match:
        return []
    month_name = (match.group("month") or match.group("month2")).lower()
    first_day = int(match.group("day") or match.group("day2"))
    days = [first_day]
    tail = line[match.end():year_match.start()]
    for value in re.findall(r"(?:&|\+|and|-)\s*(\d{1,2})(?:st|nd|rd|th)?", tail, re.I):
        day = int(value)
        if day not in days and abs(day - first_day) <= 7:
            days.append(day)
    parsed = []
    for day in days:
        try:
            parsed.append(date(year, MONTHS[month_name], day).isoformat())
        except ValueError:
            continue
    return parsed


def _time(line):
    match = TIME_VALUE.search(line)
    if not match:
        return None
    if match.group(4):
        hour, minute = int(match.group(4)), int(match.group(5))
    else:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        suffix = match.group(3).lower()
        if suffix == "pm" and hour != 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _location(date_line, following_lines):
    country_code, country_name = _country(" ".join([date_line, *following_lines[:2]]))
    if not country_code:
        return None, None, None

    location = DATE_VALUE.sub("", date_line, count=1)
    location = re.sub(r"\b(?:19|20)\d{2}\b", "", location)
    location = TIME_VALUE.sub("", location)
    location = re.sub(r"^(?:\s*(?:&|\+|and|-)\s*\d{1,2}(?:st|nd|rd|th)?)+", "", location, flags=re.I)
    location = re.sub(r"\([^)]*(?:sat|sun|mon|tue|wed|thu|fri|premiere)[^)]*\)", "", location, flags=re.I)
    location = re.sub(rf"(?<!\w){re.escape(country_name)}(?!\w)", "", location, flags=re.I)
    parts = [_clean(part) for part in re.split(r"[,|•]", location) if _clean(part)]
    parts = [part for part in parts if not re.fullmatch(r"(?:at|on|in)", part, re.I)]
    if len(parts) == 1 and re.search(r"\.\s+", parts[0]):
        venue_part, city_part = parts[0].rsplit(".", 1)
        parts = [_clean(venue_part), _clean(city_part)]

    if parts and parts[-1].casefold() in US_STATES:
        parts.pop()
    city = parts[-1] if parts else None
    venue = ", ".join(parts[:-1]) if len(parts) > 1 else None

    if city and VENUE_WORDS.search(city):
        hinted_city = next(
            (hint for hint in sorted(CITY_HINTS, key=len, reverse=True)
             if re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", city, re.I)),
            None,
        )
        if hinted_city:
            venue = ", ".join([part for part in (venue, city) if part])
            city = hinted_city

    # Sometimes the date line contains only the city and country; the venue is
    # deliberately placed on the following line.
    if city and not venue and following_lines and VENUE_WORDS.search(following_lines[0]):
        venue = _clean(following_lines[0])
        following_lines.pop(0)
    if not city and following_lines:
        next_country, next_name = _country(following_lines[0])
        if next_country == country_code:
            bits = [_clean(x) for x in following_lines.pop(0).split(",") if _clean(x)]
            bits = [x for x in bits if x.casefold() != next_name]
            if bits:
                city = bits[-1]
                venue = ", ".join(bits[:-1]) or None

    if city and VENUE_WORDS.search(city):
        hinted_city = next(
            (hint for hint in sorted(CITY_HINTS, key=len, reverse=True)
             if re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", city, re.I)),
            None,
        )
        if hinted_city:
            venue = ", ".join([part for part in (venue, city) if part])
            city = hinted_city
        else:
            city = None

    if venue and city and venue.casefold() == city.casefold():
        venue = None
    combined_location = ", ".join(part for part in (venue, city) if part)
    city_already_known = any(
        re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", city or "", re.I)
        for hint in CITY_HINTS
    )
    hinted_city = next(
        (hint for hint in sorted(CITY_HINTS, key=len, reverse=True)
         if re.search(rf"(?<!\w){re.escape(hint)}(?!\w)", combined_location, re.I)),
        None,
    )
    if hinted_city and city and not city_already_known:
        venue = combined_location
        city = hinted_city
    if venue:
        venue = re.sub(r"^(?:&|\+)\s*", "", venue).strip()
    return city, venue, country_code


def _blocks(lines):
    current = None
    for line in lines:
        if DATE_LINE.search(line) and re.search(r"\b(?:19|20)\d{2}\b", line):
            if current:
                yield current
            current = [line]
        elif current:
            current.append(line)
    if current:
        yield current


def _parse_block(block, inherited_title=None):
    header, body = block[0], [line for line in block[1:] if line]
    dates = _dates(header)
    if not dates:
        return [], None
    is_tour_heading = bool(re.search(r"\d{1,2}\s*-\s*(?:[A-Za-z]+\s+)?\d{1,2}", header))
    heading_content = [line for line in body if not re.match(r"https?://", line)]
    heading_title = inherited_title or (heading_content[0] if heading_content else None)
    city, venue, country_code = _location(header, body)
    if not all((city, venue, country_code)):
        return [], _clean(heading_title) if heading_title and is_tour_heading else None
    urls = [line for line in body if re.match(r"https?://", line)]
    content = [line for line in body if not re.match(r"https?://", line)]
    title = inherited_title or (content[0] if content else None)
    if not title:
        return [], None
    description = "\n".join([header, *content]).strip() or None
    event_url = urls[0] if urls else CALENDAR_URL
    records = [{
        "title": _clean(title),
        "date": event_date,
        "url": event_url,
        "time_from": _time(header),
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description,
    } for event_date in dates]

    # A range heading followed by a work title often introduces a tour whose
    # individual dated venue lines follow immediately.
    return records, _clean(title) if is_tour_heading else None


class DaiFujikuraCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="daifujikura_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        log_message("Fetching concert archive", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(
            CALENDAR_URL,
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"},
        )
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
        container = soup.select_one("p.paragraph-20")
        if not container:
            raise ValueError("Concert archive container was not found")
        for break_tag in container.select("br"):
            break_tag.replace_with("\n")
        lines = [_clean(line) for line in container.get_text().splitlines()]
        lines = [line for line in lines if line]

        records = []
        tour_title = None
        for block in _blocks(lines):
            parsed, new_tour_title = _parse_block(block, inherited_title=tour_title)
            records.extend(parsed)
            if new_tour_title:
                tour_title = new_tour_title
            elif parsed and len(block) > 2:
                tour_title = None

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
        )
        return records


def main():
    DaiFujikuraCrawler().run()


if __name__ == "__main__":
    main()
