import re
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Jonathan Powell"
SOURCE_URL = "https://jonathanpowell.wordpress.com/"
API_URL = "https://public-api.wordpress.com/rest/v1.1/sites/jonathanpowell.wordpress.com/posts/"

# These are the archive posts which publish actual schedules, rather than news,
# reviews, recordings, or unspecific mentions of future engagements.
SCHEDULE_POST_IDS = {58, 79, 110, 152, 153, 155}

LOCATION_RULES = [
    (r"PONCHO Concert Hall", "PONCHO Concert Hall", "Seattle", "US"),
    (r"Trinity Presbyterian Church", "Trinity Presbyterian Church", "Arvada", "US"),
    (r"Mountain View United Methodist Church", "Mountain View United Methodist Church", "Boulder", "US"),
    (r"Montview Boulevard Presbyterian Church", "Montview Boulevard Presbyterian Church", "Denver", "US"),
    (r"Spectrum(?: NY)?", "Spectrum", "New York", "US"),
    (r"Columbia College Chicago", "Columbia College Chicago", "Chicago", "US"),
    (r"Pianoforte Foundation", "Pianoforte Foundation", "Chicago", "US"),
    (r"St Nicholas.? Church", "St Nicholas’ Church", "Brighton", "GB"),
    (r"Rosslyn Hill Chapel", "Rosslyn Hill Chapel", "London", "GB"),
    (r"Jacqueline du Pr[eé] Music Building", "Jacqueline du Pré Music Building", "Oxford", "GB"),
    (r"Chethams Music School", "Chetham’s School of Music", "Manchester", "GB"),
    (r"De Toonzaal", "De Toonzaal", "'s-Hertogenbosch", "NL"),
    (r"Ereprijs studio", "Ereprijs Studio", "Apeldoorn", "NL"),
    (r"Musentempel", "Musentempel", "Karlsruhe", "DE"),
    (r"Hotel Bachmair Weissach", "Hotel Bachmair Weissach", "Weissach", "DE"),
    (r"University of Hertfordshire MayFest", "University of Hertfordshire", "Hatfield", "GB"),
    (r"Masterklass Cultural Centre", "Masterklass Cultural Centre", "Kyiv", "UA"),
    (r"Fitzwilliam College", "Fitzwilliam College", "Cambridge", "GB"),
    (r"UCL, Haldane Room", "Haldane Room, UCL", "London", "GB"),
    (r"New North London Synagogue", "New North London Synagogue", "London", "GB"),
    (r"Schott (?:Spring Piano Series, )?Schott Music", "Schott Recital Room", "London", "GB"),
    (r"Ogólnokształcąca.*?Bytom", "Fryderyk Chopin Music School", "Bytom", "PL"),
    (r"Congress Hall, Levoča", "Congress Hall", "Levoča", "SK"),
    (r"Philarmonia, Kirovograd", "Kirovohrad Philharmonia", "Kropyvnytskyi", "UA"),
    (r"Schott Recital Room", "Schott Recital Room", "London", "GB"),
    (r"JAMU, Brno", "Janáček Academy of Performing Arts", "Brno", "CZ"),
    (r"GogolFest, Kiev", "GogolFest", "Kyiv", "UA"),
    (r"Århus, Denmark", "Jonathan Powell concert at Århus", "Aarhus", "DK"),
    (r"Syddansk Musikkonservatorium\s*,\s*Esbjerg", "Syddansk Musikkonservatorium", "Esbjerg", "DK"),
    (r"Syddansk Musikkonservatorium\s*,\s*Odense", "Syddansk Musikkonservatorium", "Odense", "DK"),
    (r"Schott Recital Room\s*48 Great Marlborough Street", "Schott Recital Room", "London", "GB"),
]

MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1
)}


def _text(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    text = unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def _location(block: str):
    flattened = re.sub(r"\s+", " ", block)
    for pattern, venue, city, country_code in LOCATION_RULES:
        if re.search(pattern, flattened, re.I):
            return venue, city, country_code
    return None


def _time(block: str):
    # A bare number is much more likely to be the event's day than its time.
    match = re.search(
        r"(?<!\d)([012]?\d)(?:(?:[.:]([0-5]\d))\s*(am|pm)?|\s*(am|pm))\b",
        block,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or match.group(4) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _record(post: dict, year: int, month: int, day: int, block: str):
    location = _location(block)
    if not location or re.search(r"\bmasterclass\b", block, re.I):
        return None
    try:
        event_date = datetime(year, month, day).date().isoformat()
    except ValueError:
        return None
    venue, city, country_code = location
    title = BeautifulSoup(post["title"], "html.parser").get_text(" ", strip=True)
    return {
        "title": title or f"Jonathan Powell at {venue}",
        "date": event_date,
        "url": post["URL"],
        "time_from": _time(block),
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": block.strip(),
    }


def _parse_full_dates(post: dict, text: str, year: int):
    pattern = re.compile(
        r"(?im)^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\b"
    )
    matches = list(pattern.finditer(text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip()
        record = _record(post, year, MONTHS[match.group(2).lower()], int(match.group(1)), block)
        if record:
            records.append(record)
    return records


def _parse_may_2013(post: dict, text: str):
    # WordPress inserted line breaks inside ordinal suffixes in this post.
    text = re.sub(r"(\d{1,2})\s*\n?(?:st|nd|rd|t\s*h|th)\s*:", r"\1 May:", text, flags=re.I)
    return _parse_full_dates(post, text, 2013)


def _parse_iberia_tour(post: dict, text: str):
    pattern = re.compile(
        r"(\d{1,2})\s+(March|April)(?:,\s*7[.:]30pm\s+at)?\s*([^;]+)(?:;|\.|$)", re.I
    )
    records = []
    for match in pattern.finditer(re.sub(r"\s+", " ", text)):
        block = match.group(0)
        record = _record(post, 2014, MONTHS[match.group(2).lower()], int(match.group(1)), block)
        if record:
            record["description"] = "Albéniz: Iberia (complete cycle)\n" + block
            records.append(record)
    return records


def _parse_post(post: dict):
    text = _text(post["content"])
    post_id = post["ID"]
    if post_id == 152:
        return _parse_iberia_tour(post, text)
    if post_id == 110:
        return _parse_may_2013(post, text)
    if post_id == 58:
        # The venue and common start time appear once above the dated list.
        text = text.replace("8 April", "8 April\nSchott Recital Room, London, 6.30pm", 1)
        for day in (17, 23, 30):
            text = text.replace(
                f"{day} April", f"{day} April\nSchott Recital Room, London, 6.30pm", 1
            )
        for day in (6, 13):
            text = text.replace(
                f"{day} May", f"{day} May\nSchott Recital Room, London, 6.30pm", 1
            )
    year = {58: 2010, 79: 2012, 153: 2014, 155: 2014}[post_id]
    return _parse_full_dates(post, text, year)


class JonathanPowellCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jonathanpowell_wordpress_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "time_from", "venue", "url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching WordPress concert archive", event="crawler_url_fetch", url=API_URL)
        response = requests.get(API_URL, params={"number": 100, "page": 1}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("found", 0) > len(payload.get("posts", [])):
            raise RuntimeError("WordPress archive exceeds the requested page size")

        records = []
        for post in payload.get("posts", []):
            if post.get("ID") in SCHEDULE_POST_IDS:
                records.extend(_parse_post(post))
        log_message(
            "Parsed WordPress concert archive",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    JonathanPowellCrawler().run()


if __name__ == "__main__":
    main()
