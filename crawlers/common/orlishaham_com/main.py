import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://orlishaham.com/"
SOURCE = "Orli Shaham"
TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}


def _text(soup):
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _fetch(url):
    log_message("Fetching concert page", event="crawler_url_fetch", url=url)
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _time(value):
    return datetime.strptime(value.strip().replace(".", ""), "%I:%M %p").strftime("%H:%M:%S")


def _record(title, date, time_from, url, venue, city, country_code, description):
    return {
        "title": title.strip(),
        "date": date.strftime("%Y-%m-%d"),
        "url": url,
        "time_from": time_from,
        "time_to": None,
        "venue": venue.strip(),
        "city": city.strip(),
        "country_code": country_code,
        "description": description.strip() or None,
    }


def _json_events(soup, url):
    found = []

    def walk(value):
        if isinstance(value, dict):
            types = value.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if "Event" in types or any(str(t).endswith("Event") for t in types):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue

    records = []
    for event in found:
        start = event.get("startDate")
        location = event.get("location") or {}
        if isinstance(location, list):
            location = location[0] if location else {}
        address = location.get("address") or {} if isinstance(location, dict) else {}
        if isinstance(address, str):
            address = {}
        venue = location.get("name") if isinstance(location, dict) else None
        city = address.get("addressLocality")
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        country = {"United States": "US", "USA": "US", "Brazil": "BR"}.get(country, country)
        try:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if not all((event.get("name"), venue, city, country)):
            continue
        records.append(_record(
            event["name"], parsed, parsed.strftime("%H:%M:%S") if "T" in start else None,
            event.get("url") or url, venue, city, country,
            event.get("description") or _text(soup),
        ))
    return records


def _parse_lumos(soup, url):
    text = _text(soup)
    match = re.search(r"(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\s+([A-Z][a-z]+ \d{1,2}, \d{4}) at (\d{1,2}:\d{2}(?:am|pm))", text)
    venue = re.search(r"Location\s*\n([^\n]+)\s*\n[^\n]*,\s*New Canaan,\s*CT", text)
    title = soup.title.get_text().split("–")[0].strip()
    if not match or not venue:
        return []
    date = datetime.strptime(match.group(2), "%B %d, %Y")
    return [_record(title, date, _time(re.sub(r"(am|pm)$", r" \1", match.group(3), flags=re.I)), url,
                    venue.group(1), "New Canaan", "US", text)]


def _parse_kaufman(soup, url):
    text = _text(soup)
    match = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*\|\s*([A-Z][a-z]+ \d{1,2} \d{4})\s*\|\s*(\d{1,2}:\d{2} [ap]m)", text, re.I)
    if not match:
        return []
    date = datetime.strptime(match.group(2), "%B %d %Y")
    return [_record(soup.title.get_text().strip(), date, _time(match.group(3)), url,
                    "Merkin Hall", "New York", "US", text)]


def _parse_mso(soup, url):
    text = _text(soup)
    heading = re.search(r"Friday, ([A-Z][a-z]+ \d{1,2})\s*[–-]\s*Saturday, ([A-Z][a-z]+ \d{1,2})\s+at the ([^,\n]+),[^\n]*, Milwaukee", text)
    times = re.findall(r"(\d{1,2}:\d{2})\s*([ap])m? on (Friday|Saturday)", text, re.I)
    if not heading or len(times) < 2:
        return []
    title = soup.title.get_text().split(" - ")[0].strip()
    published = re.search(r'"datePublished":"(\d{4})-', str(soup))
    year = int(published.group(1)) if published else datetime.now().year
    records = []
    for date_text, item in zip(heading.group(1, 2), times):
        date = datetime.strptime(f"{date_text} {year}", "%B %d %Y")
        time_from = _time(f"{item[0]} {item[1]}m")
        records.append(_record(title, date, time_from, url, heading.group(3), "Milwaukee", "US", text))
    return records


def _parse_utah(soup, url):
    text = _text(soup)
    title = soup.title.get_text().split(" | ")[0].strip()
    matches = re.findall(r"(?:Friday|Saturday|Sunday|Monday|Tuesday|Wednesday|Thursday),\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+(\d{1,2}:\d{2} [AP]M)", text)
    return [_record(title, datetime.strptime(day, "%B %d, %Y"), _time(start), url,
                    "Abravanel Hall", "Salt Lake City", "US", text) for day, start in matches]


def _parse_detail(url):
    soup = _fetch(url)
    structured = _json_events(soup, url)
    if structured:
        return structured
    host = urlparse(url).netloc.lower()
    if "orchestralumos.org" in host:
        return _parse_lumos(soup, url)
    if "kaufmanmusiccenter.org" in host:
        return _parse_kaufman(soup, url)
    if "mso.org" in host:
        return _parse_mso(soup, url)
    if "utahsymphony.org" in host:
        return _parse_utah(soup, url)
    return []


class OrliShahamCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="orlishaham_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        homepage = _fetch(SOURCE_URL)
        section = homepage.select_one("#highlights")
        if section is None:
            log_message("Concert highlights section not found", event="crawler_parse_warning", url=SOURCE_URL)
            return []

        records = []
        for link in section.select("p a[href]"):
            url = urljoin(SOURCE_URL, link["href"])
            try:
                parsed = _parse_detail(url)
                if not parsed:
                    log_message("Concert detail had no complete occurrences", event="crawler_parse_warning", url=url)
                records.extend(parsed)
            except requests.RequestException as error:
                log_message("Concert detail request failed", event="crawler_request_failed", url=url,
                            error_type=type(error).__name__, error_message=str(error))
        return records


def main():
    OrliShahamCrawler().run()


if __name__ == "__main__":
    main()
