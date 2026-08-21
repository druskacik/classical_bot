import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Daniel Müller-Schott"
SOURCE_URL = "https://daniel-mueller-schott.com/"
SCHEDULE_URL = urljoin(SOURCE_URL, "concerts/")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ClassicalBot/1.0; "
        "+https://github.com/ClassicalBot)"
    )
}

COUNTRY_CODES = {
    "australia": "AU",
    "austria": "AT",
    "belgium": "BE",
    "canada": "CA",
    "china": "CN",
    "czech republic": "CZ",
    "czechia": "CZ",
    "denmark": "DK",
    "england": "GB",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "hong kong": "HK",
    "ireland": "IE",
    "italy": "IT",
    "japan": "JP",
    "latvia": "LV",
    "luxembourg": "LU",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "scotland": "GB",
    "singapore": "SG",
    "south korea": "KR",
    "spain": "ES",
    "spanien": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "taiwan": "TW",
    "united kingdom": "GB",
    "uk": "GB",
    "united states": "US",
    "usa": "US",
    "wales": "GB",
}

MONTH_ALIASES = {
    "januar": "January",
    "februar": "February",
    "märz": "March",
    "mai": "May",
    "juni": "June",
    "juli": "July",
    "oktober": "October",
    "dezember": "December",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_lines(value: str) -> list[str]:
    return [line for line in (clean_text(line) for line in value.splitlines()) if line]


def parse_date(value: str) -> str | None:
    value = clean_text(value).rstrip(".")
    parts = value.split(" ", 1)
    if parts:
        replacement = MONTH_ALIASES.get(parts[0].casefold())
        if replacement:
            value = f"{replacement} {parts[1]}"

    for date_format in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(lines: list[str]) -> str | None:
    for line in lines:
        match = re.search(
            r"Concert\s+Start\s*:\s*(\d{1,2}):?(\d{2})?\s*([ap]m)?",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            return f"{hour:02d}:{minute:02d}"
    return None


def title_from_lines(lines: list[str]) -> str:
    details = []
    for line in lines:
        if re.match(r"Concert\s+Start\s*:", line, flags=re.IGNORECASE):
            continue
        if line.casefold() == "more info":
            continue
        normalized = line.strip(" -–—")
        if normalized.casefold() == "season opening":
            continue
        details.append(normalized)
        if len(details) == 2:
            break
    detail = " — ".join(details) if details else "Concert"
    return f"Daniel Müller-Schott — {detail}"


class DanielMuellerSchottCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="daniel_mueller_schott_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="classical",
        dedupe_subset=["date", "time_from", "venue", "city"],
        front_fields=[("source_url", SCHEDULE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching concert schedule", event="crawler_url_fetch", url=SCHEDULE_URL)
        response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        records = []
        for row in soup.select("table.ssec-shortcode-calendar tr"):
            date_node = row.select_one("td.ssec-title")
            content = row.select_one("td.ssec-content")
            location_node = content.find("strong") if content else None
            if not date_node or not content or not location_node:
                continue

            date = parse_date(date_node.get_text(" ", strip=True))
            location_lines = clean_lines(location_node.get_text("\n", strip=True))
            location = location_lines[0] if location_lines else ""
            location_parts = [clean_text(part) for part in location.split(",", 2)]
            if len(location_parts) != 3:
                continue
            city, country_name, venue = location_parts
            country_code = COUNTRY_CODES.get(country_name.casefold())

            lines = clean_lines(content.get_text("\n", strip=True))
            link = content.select_one("a[href]")
            url = urljoin(SCHEDULE_URL, link.get("href", "")) if link else SCHEDULE_URL
            if not date or not city or not venue or not country_code:
                log_message(
                    "Skipping incomplete concert",
                    event="crawler_item_skipped",
                    url=url,
                    has_date=bool(date),
                    has_city=bool(city),
                    has_venue=bool(venue),
                    has_country_code=bool(country_code),
                )
                continue

            description_lines = lines[1:] if lines and lines[0] == location else lines
            description_lines = [line for line in description_lines if line.casefold() != "more info"]
            description = "\n".join(description_lines) or None
            records.append(
                {
                    "title": title_from_lines(description_lines),
                    "date": date,
                    "url": url,
                    "time_from": parse_time(description_lines),
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                }
            )

        log_message(
            "Concert schedule parsed",
            event="crawler_scrape_completed",
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    DanielMuellerSchottCrawler().run()


if __name__ == "__main__":
    main()
