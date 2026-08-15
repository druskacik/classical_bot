import hashlib
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Vox Luminis"
SOURCE_URL = "https://voxluminis.com/en/"
AGENDA_URLS = (
    "https://voxluminis.com/en/agenda/",
    "https://voxluminis.com/en/agenda/archive/",
)

COUNTRY_CODES = {
    "Australia": "AU",
    "Austria": "AT",
    "Belgium": "BE",
    "Canada": "CA",
    "France": "FR",
    "Germany": "DE",
    "Luxembourg": "LU",
    "Spain": "ES",
    "Switzerland": "CH",
    "The Netherlands": "NL",
    "United Kingdom": "GB",
    "USA": "US",
}


def _clean_text(node: Tag) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _event_url(row: Tag, page_url: str, identity: str) -> str:
    link = row.select_one("td:nth-of-type(4) a[href]")
    if link and link.get("href", "").strip():
        return urljoin(page_url, link["href"].strip())
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"{page_url}#event-{digest}"


def _parse_page(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    year = None

    for row in soup.select("table tr.el-item"):
        if "yearheader" in row.get("class", []):
            heading = row.find("h3")
            year = heading.get_text(strip=True) if heading else None
            continue

        cells = row.find_all("td", recursive=False)
        if len(cells) < 3 or not year:
            continue

        date_text = _clean_text(cells[0])
        country_name = _clean_text(cells[1])
        content = cells[2].select_one(".el-content")
        strong = content.find("strong") if content else None
        if not date_text or country_name not in COUNTRY_CODES or not content or not strong:
            continue

        try:
            parsed_date = datetime.strptime(f"{date_text.split(',')[0]}, {year}", "%B %d, %Y")
        except ValueError:
            continue

        time_match = re.search(r",\s*(\d{1,2}:\d{2})", date_text)
        location_lines = [
            re.sub(r"\s+", " ", part).strip()
            for part in strong.get_text("\n", strip=True).splitlines()
            if part.strip()
        ]
        if not location_lines or "," not in location_lines[0]:
            continue
        venue, city = [part.strip() for part in location_lines[0].rsplit(",", 1)]
        if not venue or not city:
            continue

        paragraphs = content.find_all("p", recursive=False)
        title = ""
        for paragraph in paragraphs:
            if paragraph.find("strong"):
                continue
            clone = BeautifulSoup(str(paragraph), "html.parser").p
            for performer in clone.find_all(["em", "i"]):
                performer.decompose()
            title = _clean_text(clone)
            if title:
                break
        if not title:
            continue

        description = _clean_text(content)
        identity = "|".join((parsed_date.date().isoformat(), time_match.group(1) if time_match else "", venue, city, title))
        records.append({
            "title": title,
            "date": parsed_date.date().isoformat(),
            "url": _event_url(row, page_url, identity),
            "time_from": time_match.group(1) if time_match else None,
            "time_to": None,
            "venue": venue,
            "city": city,
            "country_code": COUNTRY_CODES[country_name],
            "description": description or None,
        })

    return records


class VoxLuminisCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="voxluminis_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="BE",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        records = []
        session = requests.Session()
        session.headers.update({"User-Agent": "classical-concert-crawler/1.0"})

        for url in AGENDA_URLS:
            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                page_records = _parse_page(response.text, url)
                records.extend(page_records)
                log_message(
                    "Parsed Vox Luminis agenda page",
                    event="crawler_page_parsed",
                    url=url,
                    record_count=len(page_records),
                )
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch Vox Luminis agenda page",
                    event="crawler_url_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return records


def main():
    VoxLuminisCrawler().run()


if __name__ == "__main__":
    main()
