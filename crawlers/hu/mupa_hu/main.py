import re
import unicodedata
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Müpa Budapest"
SOURCE_URL = "https://mupa.hu/"
SITEMAP_URL = "https://mupa.hu/sitemap/programs.xml"

# Müpa is a mixed arts centre.  These four first-party genres can all contain
# events accepted by the project (opera, ballet, family concerts and orchestral
# crossover), so the complete candidate feed is sent to the classifier.
GENRES = {
    "komolyzene-opera-szinhaz",
    "vilagzene-jazz-konnyuzene",
    "tanc-ujcirkusz",
    "csaladi-es-ifjusagi-programok",
}

VENUES = {
    "bbnh": "Bartók Béla Nemzeti Hangversenyterem",
    "fesztivalszinhaz": "Fesztivál Színház",
    "uvegterem": "Üvegterem",
    "kek-terem": "Kék terem",
    "lepcsoterem": "Lépcsőterem",
    "eloadoterem": "Előadóterem",
    "zaszloter": "Zászlótér",
    "mupa-home": "Müpa Home",
    "mupa-sator": "Müpa Sátor",
    "elocsarnok": "Előcsarnok",
    "bohem-rendezvenyhelyszin": "Bohém Rendezvényhelyszín",
    "atrium-elocsarnok": "Átrium előcsarnok",
    "fesztival-ter": "Fesztivál tér",
    "millenaris": "Millenáris",
    "millenaris-uvegcsarnok-d-epulet": "Millenáris Üvegcsarnok, D épület",
    "kocsis-zoltan-terem": "Kocsis Zoltán terem",
    "kulter": "Müpa kültér",
    "ludwig-kiallitoter": "Ludwig kiállítótér",
    "cafe-sator": "Café sátor",
    "kulteri-parkolo": "Müpa kültéri parkoló",
}

URL_PATTERN = re.compile(
    r"^(?P<title>.+)-(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2})-(?P<venue>.+)$"
)


def _title_from_slug(value):
    words = unquote(value).replace("-", " ").split()
    title = " ".join(words)
    return title[:1].upper() + title[1:]


def _is_hungarian_program_url(url):
    parts = urlparse(url).path.strip("/").split("/")
    return len(parts) == 3 and parts[0] == "program" and parts[1] in GENRES


def parse_program_url(url):
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) != 3:
        return None
    match = URL_PATTERN.match(parts[2])
    if not match:
        return None

    venue = VENUES.get(match.group("venue"))
    if not venue:
        return None

    try:
        # This also rejects impossible calendar dates.
        from datetime import date

        date.fromisoformat(match.group("date"))
    except ValueError:
        return None

    return {
        "title": _title_from_slug(match.group("title")),
        "date": match.group("date"),
        "url": url,
        "time_from": match.group("time").replace("-", ":"),
        "time_to": None,
        "venue": venue,
        "city": "Budapest",
        "description": None,
    }


class MupaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="mupa_hu",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="HU",
        upload_target="potential",
        dedupe_subset=["url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching programme sitemap", event="crawler_url_fetch", url=SITEMAP_URL)
        response = requests.get(SITEMAP_URL, timeout=90)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "xml")

        records = []
        skipped_unknown_venue = 0
        for location in soup.find_all("loc"):
            url = unicodedata.normalize("NFC", location.get_text(strip=True))
            if not _is_hungarian_program_url(url):
                continue
            record = parse_program_url(url)
            if record is None:
                skipped_unknown_venue += 1
                continue
            records.append(record)

        log_message(
            "Programme sitemap parsed",
            event="crawler_scrape_completed",
            record_count=len(records),
            skipped_unknown_venue=skipped_unknown_venue,
        )
        return records


def main():
    MupaCrawler().run()


if __name__ == "__main__":
    main()
