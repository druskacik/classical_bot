import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Dobrawa Czocher"
SOURCE_URL = "https://dobrawaczocher.com/"
CONCERTS_URL = "https://dobrawaczocher.com/concerts/"
API_URL = "https://dobrawaczocher.com/wp-json/wp/v2/pages"
DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
LOCATION_RE = re.compile(r"^(.+?),\s*([A-Z]{2})$")


class DobrawaCzocherCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dobrawaczocher_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="PL",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["date", "url"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert page", event="crawler_url_fetch", url=API_URL)
        try:
            response = requests.get(
                API_URL,
                params={"slug": "concerts", "_fields": "link,content"},
                timeout=30,
            )
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                "Concert page fetch failed",
                event="crawler_url_fetch_failed",
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        if not pages:
            log_message(
                "Concert page was not returned",
                event="crawler_parse_warning",
                url=CONCERTS_URL,
            )
            return []

        page = pages[0]
        soup = BeautifulSoup(page["content"]["rendered"], "html.parser")
        heading = soup.find(["h1", "h2", "h3", "h4"])
        series = heading.get_text(" ", strip=True) if heading else "Concert"
        records = []

        for item in soup.select("li"):
            text = " ".join(item.stripped_strings)
            date_match = DATE_RE.search(text)
            link = item.find("a", href=True)
            if not date_match or not link:
                continue

            location_match = next(
                (
                    LOCATION_RE.match(value)
                    for value in reversed(list(item.stripped_strings))
                    if LOCATION_RE.match(value)
                ),
                None,
            )
            if not location_match:
                log_message(
                    "Skipping concert without a city and country",
                    event="crawler_parse_warning",
                    url=link["href"],
                )
                continue

            try:
                concert_date = datetime.strptime(date_match.group(1), "%d.%m.%Y").date().isoformat()
            except ValueError:
                log_message(
                    "Skipping concert with an invalid date",
                    event="crawler_parse_warning",
                    url=link["href"],
                )
                continue

            linked_label = link.get_text(" ", strip=True)
            is_album_premiere = bool(re.search(r"\bALBUM PREMIERE$", linked_label, re.I))
            venue = re.sub(r"\s+ALBUM PREMIERE$", "", linked_label, flags=re.I).strip()
            if not venue:
                continue

            title = f"{SOURCE} — {series}"
            if is_album_premiere:
                title += " — Album Premiere"

            records.append(
                {
                    "title": title,
                    "date": concert_date,
                    "url": link["href"],
                    "time_from": None,
                    "venue": venue,
                    "city": location_match.group(1).strip(),
                    "country_code": location_match.group(2),
                    "description": text,
                }
            )

        log_message(
            "Parsed concert page",
            event="crawler_parse_completed",
            url=page.get("link", CONCERTS_URL),
            record_count=len(records),
        )
        return records


def main():
    DobrawaCzocherCrawler().run()


if __name__ == "__main__":
    main()
