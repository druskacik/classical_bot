import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Friends of Chamber Music of Miami"
SOURCE_URL = "https://www.miamichambermusic.org/"
SEASON_URL = urljoin(SOURCE_URL, "2026-2027-season")
TIMEOUT = 45
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)",
    "Accept-Language": "en-US,en;q=0.9",
}

DATE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\s*[·,]?\s*"
    r"(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2}),\s*(?P<year>20\d{2})",
    re.IGNORECASE,
)
ARCHIVE_DATE_RE = re.compile(
    r"(?P<month>[A-Za-z]+)\.?\s+(?P<day>\d{1,2}),\s*(?P<year>20\d{2})"
    r"\s*(?:—|–|-|\bat\b)\s*(?P<venue>.+)$",
    re.IGNORECASE,
)
SPECIAL_RE = re.compile(
    r"WHEN\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
    r"(?P<date>[A-Za-z]+\s+\d{1,2},\s+20\d{2})\s*[·•]\s*"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+WHERE\s+(?P<venue>.+?)(?:\s+RESERVE|\s+PROGRAM)",
    re.IGNORECASE,
)

VENUES = {
    "FIU": ("FIU Wertheim Performing Arts Center", "Miami"),
    "FIU Wertheim Performing Arts Center": ("FIU Wertheim Performing Arts Center", "Miami"),
    "Coral Gables Congregational Church": ("Coral Gables Congregational Church", "Coral Gables"),
    "CCGC": ("Coral Gables Congregational Church", "Coral Gables"),
    "UM Knight Center for Music Innovation": ("UM Knight Center for Music Innovation", "Coral Gables"),
    "Bet Shira Congregation": ("Bet Shira Congregation", "Miami"),
    "Temple Bet Shira": ("Bet Shira Congregation", "Miami"),
    "Coral Gables Steinway Piano Gallery": ("Coral Gables Steinway Piano Gallery", "Coral Gables"),
}


def clean_text(value):
    if value is None:
        return ""
    if hasattr(value, "get_text"):
        value = value.get_text("\n", strip=True)
    value = str(value).replace("\xa0", " ").replace("\u202f", " ").replace("\ufeff", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_date(month, day, year):
    for pattern in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{month} {day} {year}", pattern).date().isoformat()
        except ValueError:
            pass
    return None


def resolve_venue(value):
    value = clean_text(value).strip(" .")
    for label, result in VENUES.items():
        if value.casefold() == label.casefold():
            return result
    return None


def fetch(session, url):
    log_message("Fetching concert page", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_current_season(soup, url=SEASON_URL):
    records = []
    for card in soup.select("main .card"):
        title_node = card.select_one("h2.title, h2")
        lines = clean_text(card).splitlines()
        date_match = next((DATE_RE.search(line) for line in lines if DATE_RE.search(line)), None)
        if not title_node or not date_match:
            continue
        location = resolve_venue(lines[-1])
        event_date = parse_date(
            date_match.group("month"), date_match.group("day"), date_match.group("year")
        )
        if not event_date or not location:
            continue
        title = clean_text(title_node)
        description_lines = [line for line in lines if line not in {title, lines[0], lines[-1]}]
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": None,
                "venue": location[0],
                "city": location[1],
                "description": "\n".join(description_lines) or None,
            }
        )
    return records


def parse_archive(soup, url):
    records = []
    paragraphs = soup.select("main .sqs-html-content p")
    for index, paragraph in enumerate(paragraphs):
        text = clean_text(paragraph).replace("\n", " ")
        match = ARCHIVE_DATE_RE.search(text)
        if not match:
            continue
        location = resolve_venue(match.group("venue"))
        event_date = parse_date(match.group("month"), match.group("day"), match.group("year"))
        title = text[: match.start()].strip(" —–-")
        if not title or not event_date or not location:
            continue
        description = None
        if index + 1 < len(paragraphs):
            candidate = clean_text(paragraphs[index + 1])
            if candidate and not ARCHIVE_DATE_RE.search(candidate.replace("\n", " ")):
                description = candidate
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": url,
                "time_from": None,
                "venue": location[0],
                "city": location[1],
                "description": description,
            }
        )
    return records


def parse_home_special(soup):
    main = soup.select_one("main")
    heading = main.select_one("h2") if main else None
    text = clean_text(main).replace("\n", " ") if main else ""
    match = SPECIAL_RE.search(text)
    if not heading or not match:
        return []
    location = resolve_venue(match.group("venue"))
    if not location:
        return []
    try:
        event_date = datetime.strptime(match.group("date"), "%B %d, %Y").date().isoformat()
        event_time = datetime.strptime(match.group("time").upper(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return []
    ticket = main.select_one('a[href*="/concert-tickets/p/"]')
    program_match = re.search(r"\bPROGRAM\b\s*(.+?)\s*\bOUR 71ST\b", text, re.IGNORECASE)
    program = program_match.group(1) if program_match else None
    return [
        {
            "title": clean_text(heading),
            "date": event_date,
            "url": ticket.get("href") if ticket else SOURCE_URL,
            "time_from": event_time,
            "venue": location[0],
            "city": location[1],
            "description": clean_text(program) or None,
        }
    ]


class MiamiChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="miamichambermusic_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        home = fetch(session, SOURCE_URL)
        records = parse_home_special(home)
        season_anchor = home.select_one('a[href*="-season"]')
        season_url = urljoin(SOURCE_URL, season_anchor["href"]) if season_anchor else SEASON_URL
        records.extend(parse_current_season(fetch(session, season_url), season_url))

        archive_urls = sorted(
            {
                urljoin(SOURCE_URL, anchor["href"])
                for anchor in home.select('a[href*="season-recap-"]')
            }
        )
        for url in archive_urls:
            records.extend(parse_archive(fetch(session, url), url))

        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["title"]))
        log_message(
            "Concert pages parsed",
            event="crawler_parse_completed",
            record_count=len(records),
        )
        return records


def main():
    MiamiChamberMusicOrgCrawler().run()


if __name__ == "__main__":
    main()
