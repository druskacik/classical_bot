import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.sophiajaffe.com/"
SOURCE = "Sophia Jaffé"
CALENDAR_URL = urljoin(SOURCE_URL, "kalender")
ARCHIVE_URL = urljoin(SOURCE_URL, "archiv")

COUNTRY_NAMES = {
    "deutschland": "DE", "germany": "DE", "österreich": "AT",
    "austria": "AT", "schweiz": "CH", "switzerland": "CH",
    "irland": "IE", "ireland": "IE", "estland": "EE",
    "usa": "US", "united states": "US", "uk": "GB",
    "england": "GB", "slovakia": "SK", "slowakei": "SK",
    "tschechien": "CZ", "czech republic": "CZ", "norway": "NO",
    "norwegen": "NO", "france": "FR", "frankreich": "FR",
    "italy": "IT", "italien": "IT", "netherlands": "NL",
    "niederlande": "NL", "belgium": "BE", "belgien": "BE",
    "romania": "RO", "rumania": "RO", "rumänien": "RO",
    "poland": "PL", "polen": "PL", "spain": "ES", "spanien": "ES",
}
COUNTRY_CODES = {
    "DE": "DE", "AT": "AT", "CH": "CH", "IE": "IE", "EE": "EE",
    "US": "US", "USA": "US", "UK": "GB", "GB": "GB", "SK": "SK",
    "CZ": "CZ", "NO": "NO", "FR": "FR", "IT": "IT", "NL": "NL",
    "BE": "BE", "RO": "RO", "PL": "PL", "ES": "ES",
}
VENUE_WORDS = re.compile(
    r"(?i)(saal|hall\b|halle\b|theater|theatre|auditorium|kirche|church|"
    r"schloss|stift\b|tonhalle|philharmoni|oper\b|festival|museum|zentrum|"
    r"center|centre|konzerthaus|hochschule|hfmdk|academy|palais|kapelle|"
    r"synagoge|bibliothek|studio|institut|college|university|haus\b|burg\b)"
)
DATE_RE = re.compile(
    r"^(?P<start>\d{1,2}\.\d{1,2}\.\d{4})"
    r"(?:\s*(?:—|–|-)\s*(?P<end>\d{1,2}\.\d{1,2}\.\d{4}))?"
)
SHORT_RANGE_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|Uhr)\b", re.I)


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip(" \n,–—-")


def parse_date(value):
    match = DATE_RE.match(value)
    if match:
        return datetime.strptime(match.group("start"), "%d.%m.%Y").date().isoformat()
    match = SHORT_RANGE_RE.match(value)
    if match:
        return datetime(int(match.group(4)), int(match.group(3)), int(match.group(1))).date().isoformat()
    return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = match.group(3).lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def country_from(value):
    normalized = clean(value).lower()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf"(?:^|[,( /]){re.escape(name)}(?:$|[) /])", normalized):
            return code
    tokens = re.findall(r"\b[A-Z]{2,3}\b", value)
    for token in reversed(tokens):
        if token in COUNTRY_CODES:
            return COUNTRY_CODES[token]
    return None


def city_from(value):
    value = clean(value)
    value = re.sub(r"\s*\([^)]*(?:USA|US|DE|AT|CH|GB|UK)\)\s*$", "", value, flags=re.I)
    value = re.sub(r",?\s+(?:" + "|".join(map(re.escape, COUNTRY_NAMES)) + r")\s*$", "", value, flags=re.I)
    value = re.sub(r",?\s+(?:DE|AT|CH|IE|EE|US|USA|UK|GB|SK|CZ|NO|FR|IT|NL|BE|RO|PL|ES)\s*$", "", value)
    value = re.sub(r",\s*(?:California|Tirol)\s*$", "", value, flags=re.I)
    value = clean(value.split(" — ")[-1])
    value = re.sub(r"\s*/\s*Tirol$", "", value, flags=re.I)
    if re.fullmatch(r"(?i)Stift Altenburg", value):
        return "Altenburg"
    return value


def looks_like_venue(value):
    return bool(VENUE_WORDS.search(value)) and not re.search(
        r"(?i)(leitung|dirigent|violine|viola|cello|klavier|sopran|klarinette)", value
    )


def title_from(lines):
    for line in lines:
        candidate = clean(line)
        if not candidate or looks_like_venue(candidate):
            continue
        if re.fullmatch(r"\d{1,2}(?::\d{2})?\s*(?:am|pm|Uhr),?", candidate, re.I):
            continue
        if re.search(r"(?i)(violine|viola|cello|klavier|leitung|dirigent)", candidate) and ":" not in candidate:
            continue
        return candidate[:500]
    return None


def make_record(*, date, title, url, time_from, venue, city, country_code, description):
    if not all((date, title, url, venue, city, country_code)):
        return None
    return {
        "title": title,
        "date": date,
        "url": url,
        "time_from": time_from,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": description or None,
    }


def parse_featured_items(soup):
    records = []
    for item in soup.select(".list-item-content"):
        heading = item.select_one(".list-item-content__title")
        description_node = item.select_one(".list-item-content__description")
        if not heading or not description_node:
            continue
        date = parse_date(clean(heading.get_text(" ", strip=True)).rstrip("."))
        lines = [clean(x) for x in description_node.get_text("\n", strip=True).splitlines() if clean(x)]
        description = "\n".join(lines)
        venue = next((line for line in lines if looks_like_venue(line)), None)
        city = None
        country = country_from(description)
        if venue and re.search(r"(?i)stift altenburg", venue):
            city, country = "Altenburg", country or "AT"
        for line in reversed(lines):
            if country_from(line):
                city, country = city_from(line), country_from(line)
                break
        link = item.select_one("a[href]")
        title = title_from([line for line in lines if line != venue])
        record = make_record(
            date=date, title=title, url=urljoin(CALENDAR_URL, link["href"]) if link else CALENDAR_URL,
            time_from=parse_time(description), venue=venue, city=city, country_code=country,
            description=description,
        )
        if record:
            records.append(record)
    return records


def parse_history_blocks(soup):
    records = []
    for block in soup.select(".sqs-html-content"):
        lines = [clean(x) for x in block.get_text("\n", strip=True).splitlines() if clean(x)]
        if not lines or not (parse_date(lines[0]) or SHORT_RANGE_RE.match(lines[0])):
            continue
        date = parse_date(lines[0])
        location = lines[-1]
        country = country_from(location)
        city = city_from(location) if country else None
        venue = next((line for line in reversed(lines[1:-1]) if looks_like_venue(line)), None)
        title = title_from(lines[1:-1])
        record = make_record(
            date=date, title=title, url=CALENDAR_URL, time_from=parse_time(lines[0]),
            venue=venue, city=city, country_code=country, description="\n".join(lines[1:]),
        )
        if record:
            records.append(record)
    return records


def archive_groups(soup):
    block = next(
        (b for b in soup.select(".sqs-html-content") if re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", b.get_text())),
        None,
    )
    if not block:
        return []
    groups = []
    current = None
    for paragraph in block.select("p"):
        lines = [clean(line) for line in paragraph.get_text("\n", strip=True).splitlines() if clean(line)]
        if not lines:
            continue
        text = " ".join(lines)
        if DATE_RE.match(text):
            if current:
                groups.append(current)
            current = {"header": text, "parts": [], "links": []}
        elif current:
            current["parts"].extend(
                line for line in lines
                if not re.fullmatch(r"[A-Za-zÄÖÜäöü]{3,9}\s+\d{4}", line)
            )
            current["links"].extend(a.get("href") for a in paragraph.select("a[href]"))
    if current:
        groups.append(current)
    return groups


def parse_archive(soup):
    records = []
    for group in archive_groups(soup):
        header, parts = group["header"], group["parts"]
        date = parse_date(header)
        country = country_from(header)
        city = None
        location_match = re.search(r"(?:am|pm)\s*—\s*(.+)$", header, re.I)
        if location_match:
            city = city_from(location_match.group(1).split(" — ", 1)[0])
        if not country:
            country = country_from(" ".join(parts))
        venue = next((clean(line.lstrip("@ ")) for line in parts if looks_like_venue(line)), None)
        if venue and venue.startswith("@"):
            venue = clean(venue[1:])
        if venue and " / " in venue:
            venue = clean(venue.split(" / ", 1)[0])
        if city and re.search(r"(?i)festival|concert|konzert|masterclass", city) and venue:
            city = clean(venue.split(",", 1)[0])
        title = title_from(parts)
        url = next((urljoin(ARCHIVE_URL, link) for link in group["links"] if link), ARCHIVE_URL)
        record = make_record(
            date=date, title=title, url=url, time_from=parse_time(header), venue=venue,
            city=city, country_code=country, description="\n".join(parts),
        )
        if record:
            records.append(record)
    return records


class SophiaJaffeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sophiajaffe_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def fetch(self, url):
        log_message("Fetching concert page", event="crawler_url_fetch", url=url)
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")

    def scrape(self):
        calendar = self.fetch(CALENDAR_URL)
        archive = self.fetch(ARCHIVE_URL)
        records = parse_featured_items(calendar) + parse_history_blocks(calendar) + parse_archive(archive)
        log_message("Parsed concert records", event="crawler_records_parsed", record_count=len(records))
        return records


def main():
    SophiaJaffeCrawler().run()


if __name__ == "__main__":
    main()
