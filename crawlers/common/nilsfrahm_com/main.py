import re
from datetime import date
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Nils Frahm"
SOURCE_URL = "https://www.nilsfrahm.com/"
SITEMAP_URL = f"{SOURCE_URL}wp-sitemap-posts-concert-1.xml"
DATE_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})(?:-\d+)?$")

# The site has used several informal country abbreviations in concert slugs.
COUNTRY_CODES = {
    "a": "AT", "aus": "AU", "b": "BE", "be": "BE", "bel": "BE",
    "can": "CA", "ch": "CH", "cz": "CZ", "d": "DE", "dk": "DK",
    "ee": "EE", "eir": "IE", "esp": "ES", "f": "FR", "fin": "FI",
    "geo": "GE", "gr": "GR", "hr": "HR", "hu": "HU", "isr": "IL",
    "it": "IT", "lt": "LT", "lux": "LU", "lv": "LV", "nl": "NL",
    "no": "NO", "pl": "PL", "pt": "PT", "se": "SE", "slo": "SI",
    "tk": "TR", "tw": "TW", "uk": "GB", "us": "US", "usa": "US",
}

REGIONS = {
    "ab", "bc", "ca", "co", "il", "md", "ny", "oh", "on", "or",
    "qc", "tn", "wa",
}

# Multi-word cities seen in this first-party archive. Single-word cities are
# handled generically, while these need an explicit boundary from the venue.
MULTIWORD_CITIES = sorted({
    "byron-bay", "cella-monte-monferrato", "den-haag", "frankfurt-am-main",
    "gardone-riviera", "las-vegas", "los-angeles", "new-york",
    "new-york-city", "north-bethesda", "san-francisco", "san-fransisco",
    "santiago-de-compostela", "tel-aviv", "yverdon-les-bains",
}, key=len, reverse=True)


def _label(value: str) -> str:
    return " ".join(word.upper() if word in {"bbc", "kkl", "nch", "nfm", "nospr", "qpac", "ukk"}
                    else word.capitalize() for word in value.split("-"))


def _parse_url(url: str) -> dict | None:
    slug = unquote(urlparse(url).path.rstrip("/").split("/")[-1]).lower()
    match = DATE_RE.search(slug)
    if not match:
        return None
    try:
        event_date = date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None

    location_slug = slug[:match.start()]
    parts = location_slug.split("-")
    if not parts or parts[-1] not in COUNTRY_CODES:
        return None
    country_code = COUNTRY_CODES[parts.pop()]
    region = parts[-1] if parts and parts[-1] in REGIONS else None
    if region:
        parts.pop()
        # One archived URL says "Vancouver-bc-usa". The explicit Canadian
        # province is stronger geography than that erroneous country suffix.
        if region in {"ab", "bc", "on", "qc"}:
            country_code = "CA"

    remainder = "-".join(parts)
    city_slug = next((city for city in MULTIWORD_CITIES if remainder.endswith(f"-{city}")), None)
    if city_slug:
        venue_slug = remainder[:-(len(city_slug) + 1)]
    elif len(parts) >= 2:
        city_slug = parts[-1]
        venue_slug = "-".join(parts[:-1])
    else:
        return None
    if not venue_slug or venue_slug.startswith("auto-draft") or city_slug in {"park"}:
        return None

    venue = _label(venue_slug)
    city = _label(city_slug)
    return {
        "title": f"Nils Frahm at {venue}",
        "date": event_date,
        "url": url,
        "time_from": None,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": None,
    }


class NilsFrahmCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nilsfrahm_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["url"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert sitemap", event="crawler_url_fetch", url=SITEMAP_URL)
        response = requests.get(SITEMAP_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "xml")
        urls = [node.get_text(strip=True) for node in soup.find_all("loc")]
        records = [record for url in urls if (record := _parse_url(url)) is not None]
        log_message(
            "Concert sitemap parsed",
            event="crawler_scrape_completed",
            url=SITEMAP_URL,
            record_count=len(records),
            skipped_count=len(urls) - len(records),
        )
        return records


def main():
    NilsFrahmCrawler().run()


if __name__ == "__main__":
    main()
