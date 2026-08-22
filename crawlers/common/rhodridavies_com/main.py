import re
from collections import Counter
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://rhodridavies.com/"
SOURCE = "Rhodri Davies"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ,-|\\")


def _parse_date(text: str, default_year: int | None) -> str | None:
    match = re.search(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"(?:\s+(\d{4}))?\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else default_year
    if year is None:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)} {year}", "%d %B %Y"
        ).date().isoformat()
    except ValueError:
        return None


def _parse_time(text: str) -> tuple[str | None, str | None]:
    matches = re.findall(
        r"(?<!\d)(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)(?!\w)", text, re.IGNORECASE
    )
    values = []
    for hour, minute, meridiem in matches[:2]:
        hour_value = int(hour) % 12 + (12 if meridiem.lower() == "pm" else 0)
        values.append(f"{hour_value:02d}:{int(minute or 0):02d}")
    return (values[0] if values else None, values[1] if len(values) > 1 else None)


def _location(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract only locations for which the artist page supplies defensible evidence."""
    rules = (
        (r"\bCAFE OTO\b", "CAFE OTO", "London", "GB"),
        (r"\bThe Old Church,\s*Stoke Newington\b", "The Old Church", "London", "GB"),
        (r"\bHorse Hospital\b", "Horse Hospital", "London", "GB"),
        (r"\bHome Bar,\s*Edinburgh\b", "Home Bar", "Edinburgh", "GB"),
    )
    for pattern, venue, city, country_code in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return venue, city, country_code
    return None, None, None


def _title(block, date_text: str) -> str | None:
    strong = block.find("strong")
    heading = _clean(strong.get_text(" ", strip=True)) if strong else ""
    before_date = _clean(heading.split(date_text, 1)[0]) if date_text in heading else ""
    before_date = re.sub(
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$",
        "",
        before_date,
        flags=re.IGNORECASE,
    ).strip()
    if before_date:
        return before_date

    if strong and strong.parent:
        combined = _clean(strong.parent.get_text(" ", strip=True))
        trailing = _clean(combined.removeprefix(heading))
        if trailing:
            return trailing

    spans = block.find_all("span", class_="p")
    for span in spans:
        if strong and span.find("strong"):
            continue
        value = _clean(span.get_text(" ", strip=True))
        if value and not value.lower().startswith(("tickets", "[tickets")):
            return value
    return None


class RhodriDaviesCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="rhodridavies_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching events page", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        section = soup.select_one("#events-section")
        if section is None:
            log_message("Events section not found", event="crawler_parse_warning", url=SOURCE_URL)
            return []

        blocks = section.select("p.text-component")
        explicit_years = []
        for block in blocks:
            explicit_years.extend(int(year) for year in re.findall(r"\b(20\d{2})\b", block.get_text(" ")))
        default_year = Counter(explicit_years).most_common(1)[0][0] if explicit_years else None

        records = []
        for block in blocks:
            text = _clean(block.get_text(" ", strip=True))
            date = _parse_date(text, default_year)
            date_match = re.search(
                r"\b\d{1,2}(?:st|nd|rd|th)?(?:\s+of)?\s+[A-Za-z]+(?:\s+20\d{2})?\b", text
            )
            venue, city, country_code = _location(text)
            if not date or not date_match or not venue or not city or not country_code:
                continue
            title = _title(block, date_match.group(0))
            if not title:
                continue
            time_from, time_to = _parse_time(text)
            link = block.find("a", href=True)
            records.append(
                {
                    "title": title,
                    "date": date,
                    "url": link["href"] if link else f"{SOURCE_URL}#events",
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": text,
                }
            )

        log_message(
            "Parsed events page",
            event="crawler_scrape_completed",
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    RhodriDaviesCrawler().run()


if __name__ == "__main__":
    main()
