import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.jeanmuller.com/"
CALENDAR_URL = f"{SOURCE_URL}calendar"
SOURCE = "Jean Muller"

# The artist tours internationally.  These first-party calendar labels are the
# venue names actually published by the source, paired with their locality.
VENUES = {
    "philharmonie luxembourg": ("Philharmonie Luxembourg", "Luxembourg", "LU"),
    "luxembourg philharmonie": ("Philharmonie Luxembourg", "Luxembourg", "LU"),
    "centre national de littérature": ("Centre national de littérature", "Mersch", "LU"),
    "le fenil aux mille divans": ("Le fenil aux mille divans", "Herbaimont", "BE"),
    "luxembourg conservatoire": ("Conservatoire de la Ville de Luxembourg", "Luxembourg", "LU"),
    "conservatoire luxembourg": ("Conservatoire de la Ville de Luxembourg", "Luxembourg", "LU"),
    "tnl luxembourg": ("Théâtre National du Luxembourg", "Luxembourg", "LU"),
    "trifolion echternach": ("Trifolion Echternach", "Echternach", "LU"),
    "cape ettelbruck": ("CAPE Ettelbruck", "Ettelbruck", "LU"),
    "cube 521": ("Cube 521", "Marnach", "LU"),
    "kinneksbond": ("Kinneksbond", "Mamer", "LU"),
    "artikuss": ("Artikuss", "Soleuvre", "LU"),
    "musikverein": ("Musikverein", "Vienna", "AT"),
    "alte oper": ("Alte Oper", "Frankfurt", "DE"),
    "elbphilharmonie": ("Elbphilharmonie", "Hamburg", "DE"),
    "steinway haus": ("Steinway-Haus", "Frankfurt", "DE"),
    "steinway-haus": ("Steinway-Haus", "Frankfurt", "DE"),
    "schumann-gesellschaft": ("Schumann-Gesellschaft", "Frankfurt", "DE"),
    "pianosalon": ("Pianosalon Christophori", "Berlin", "DE"),
    "salle cortot": ("Salle Cortot", "Paris", "FR"),
    "automobile club de france": ("Automobile Club de France", "Paris", "FR"),
    "petit palais": ("Petit Palais", "Paris", "FR"),
    "théâtre athénée": ("Théâtre de l’Athénée", "Paris", "FR"),
    "conway hall": ("Conway Hall", "London", "GB"),
    "st. john's smith square": ("St John's Smith Square", "London", "GB"),
    "oriental art center": ("Shanghai Oriental Art Center", "Shanghai", "CN"),
    "daning theatre": ("Daning Theatre", "Shanghai", "CN"),
    "guangzhou, opera house": ("Guangzhou Opera House", "Guangzhou", "CN"),
    "new york university arts center": ("NYU Abu Dhabi Arts Center", "Abu Dhabi", "AE"),
    "stadthalle erkelenz": ("Stadthalle Erkelenz", "Erkelenz", "DE"),
    "ehrbarsaal": ("Ehrbar Saal", "Vienna", "AT"),
    "zehntscheune": ("Zehntscheune", "Freden", "DE"),
    "stavros niarchos": ("Stavros Niarchos Foundation Cultural Center", "Athens", "GR"),
    "manos hatzidakis": ("Manos Hatzidakis Garden Theatre", "Heraklion", "GR"),
    "haus beda": ("Haus Beda", "Bitburg", "DE"),
}


def _clean(text):
    return re.sub(r"\s+", " ", text).strip(" \u200b")


def _location(text):
    folded = text.casefold()
    for marker, location in VENUES.items():
        if marker in folded:
            return location
    return None


def _date(value):
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def _record(date_text, details, url=CALENDAR_URL, description=None):
    location = _location(details)
    if not location:
        return None
    venue, city, country_code = location
    title = _clean(description or re.sub(r"^.*?\s+-\s+", "", details))
    if not title or title.casefold() == venue.casefold():
        title = f"Jean Muller at {venue}"
    return {
        "title": title,
        "date": _date(date_text),
        "url": url,
        "time_from": None,
        "time_to": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": _clean(description or details),
    }


class JeanMullerCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jeanmuller_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "venue", "title"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching calendar", event="crawler_url_fetch", url=CALENDAR_URL)
        response = requests.get(CALENDAR_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        # Rich-text entries above the archive have a date/location paragraph,
        # followed by programme text and, where supplied, a first-party ticket link.
        paragraphs = soup.select("p.wixui-rich-text__text")
        for index, paragraph in enumerate(paragraphs):
            heading = _clean(paragraph.get_text(" ", strip=True))
            match = re.fullmatch(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+-\s+(.+)", heading)
            if not match:
                continue
            description = None
            event_url = CALENDAR_URL
            for following in paragraphs[index + 1:index + 4]:
                text = _clean(following.get_text(" ", strip=True))
                if re.match(r"\d{1,2}\.\d{1,2}\.\d{4}\s+-", text):
                    break
                link = following.find("a", href=True)
                if link:
                    event_url = link["href"]
                elif description is None and text and "Tickets" not in text and text != "Archive":
                    description = text
            record = _record(match.group(1), heading, event_url, description)
            if record:
                records.append(record)

        # Archives are deliberately retained by the site.  Date ranges and
        # multi-date summaries are not concrete single occurrences, so skip them.
        for archive in soup.select("p.wixui-collapsible-text__text"):
            for line in archive.get_text("\n", strip=True).splitlines():
                line = _clean(line)
                match = re.fullmatch(r"(\d{1,2}\.\d{1,2}\.\d{4})\s+-\s+(.+)", line)
                if not match or re.search(r"\b(jury|broadcast|short film)\b", line, re.I):
                    continue
                record = _record(match.group(1), line)
                if record:
                    records.append(record)

        log_message("Calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    JeanMullerCrawler().run()


if __name__ == "__main__":
    main()
