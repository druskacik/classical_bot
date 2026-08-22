import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Michael McHale"
SOURCE_URL = "https://www.michaelmchale.com/"
CONCERTS_URL = urljoin(SOURCE_URL, "concerts")

DATE_RE = re.compile(r"^(\d{1,2} [A-Za-z]+, \d{4})$")
TIME_RE = re.compile(
    r"^\s*(\d(?:\s?\d)?)(?:\s*[.:]\s*(\d{2}))?\s*(am|pm)\b\s*",
    re.IGNORECASE,
)
COUNTRIES = {
    "argentina": "AR",
    "croatia": "HR",
    "england": "GB",
    "germany": "DE",
    "ireland": "IE",
    "isle of man": "IM",
    "n ireland": "GB",
    "netherlands": "NL",
    "northern ireland": "GB",
    "scotland": "GB",
    "slovenia": "SI",
    "uk": "GB",
    "usa": "US",
    "wales": "GB",
}
US_STATE_RE = re.compile(
    r"^(?:A[KLRSZ]|C[AOT]|D[EC]|F[LM]|G[AU]|HI|I[ADLN]|K[SY]|LA|M[ABCDEHINOPST]"
    r"|N[CDEHJMVY]|O[HKR]|P[AR]|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])$",
    re.IGNORECASE,
)
COUNTY_RE = re.compile(
    r"^(?:co\.?(?:\s+|$)|county\s+|east sussex$|fermanagh$|hertfordshire$|kent$|oxfordshire$|sussex$|suffolk$)",
    re.IGNORECASE,
)

# These venue calendars are named on the page without their municipality.
# The mappings avoid applying the artist's home location to touring dates.
VENUE_CITIES = {
    "ardhowen theatre": "Enniskillen",
    "clandeboye festival": "Bangor",
    "st albans abbey": "St Albans",
    "waterford city hall": "Waterford",
}
VENUE_NAMES = {"clandeboye festival": "Clandeboye Estate"}


def _clean(text):
    text = text.replace("\u200b", " ").replace("\ufeff", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _time_and_title(text):
    match = TIME_RE.match(text)
    if not match:
        return None, text
    hour = int(match.group(1).replace(" ", ""))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None, text
    if match.group(3).lower() == "pm" and hour != 12:
        hour += 12
    if match.group(3).lower() == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}", text[match.end():].strip()


def _location(text):
    parts = [_clean(part) for part in text.split(",") if _clean(part)]
    if len(parts) < 2:
        return None

    country_key = re.sub(r"[.]", "", parts[-1]).lower()
    country_code = COUNTRIES.get(country_key)
    if not country_code:
        return None

    body = parts[:-1]
    if country_code == "US":
        if len(body) < 3 or not US_STATE_RE.match(body[-1]):
            return None
        city = body[-2]
        venue_parts = body[:-2]
    elif country_code == "IE":
        if body and COUNTY_RE.match(body[-1]):
            county = re.sub(r"^co\.?(?:\s+|$)", "", body.pop(), flags=re.IGNORECASE)
            if len(body) >= 2:
                city = body.pop()
            elif body:
                venue_key = body[0].lower()
                city = VENUE_CITIES.get(venue_key, county)
            else:
                return None
        elif len(body) >= 2:
            city = body.pop()
        else:
            return None
        venue_parts = body
    else:
        if body and COUNTY_RE.match(body[-1]):
            body.pop()
            if len(body) >= 2:
                city = body.pop()
            elif body:
                city = VENUE_CITIES.get(body[0].lower())
                if not city:
                    return None
            else:
                return None
        elif len(body) >= 2:
            city = body.pop()
        else:
            return None
        venue_parts = body

    venue = ", ".join(venue_parts).strip()
    if not venue or not city or venue.casefold() == city.casefold():
        return None
    venue = VENUE_NAMES.get(venue.casefold(), venue)
    return venue, city, country_code


def _blocks(container):
    current = None
    for paragraph in container.find_all("p"):
        text = _clean(paragraph.get_text(" ", strip=True))
        date_match = DATE_RE.fullmatch(text)
        if date_match:
            if current:
                yield current
            current = {"date_text": date_match.group(1), "lines": [], "url": None}
            continue
        if current and text:
            current["lines"].append(text)
            link = paragraph.find("a", href=True)
            if link and not current["url"]:
                current["url"] = urljoin(CONCERTS_URL, link["href"])
    if current:
        yield current


def _record(block):
    lines = block["lines"]
    if len(lines) < 2:
        return None
    full_text = "\n".join(lines)
    if re.search(r"\b(?:private|recording session)\b", full_text, re.IGNORECASE):
        return None

    location = _location(lines[-1])
    if not location:
        return None
    venue, city, country_code = location

    title_line = next(
        (line for line in lines[:-1] if not re.fullmatch(r"\*+[^*]*cancelled[^*]*\*+", line, re.IGNORECASE)),
        "",
    )
    time_from, title = _time_and_title(title_line)
    if not title:
        return None
    try:
        event_date = datetime.strptime(block["date_text"], "%d %B, %Y").date().isoformat()
    except ValueError:
        return None

    return {
        "title": title,
        "date": event_date,
        "url": block["url"] or CONCERTS_URL,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": full_text,
    }


class MichaelMcHaleCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="michaelmchale_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="GB",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert listing", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        containers = []
        for element_id in ("WRchTxt1", "comp-l5mh6egk"):
            container = soup.find(id=element_id)
            if container:
                containers.append(container)
        if not containers:
            raise ValueError("Concert listing containers were not found")

        records = []
        for container in containers:
            records.extend(record for block in _blocks(container) if (record := _record(block)))
        log_message(
            "Parsed concert listing",
            event="crawler_parse_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    MichaelMcHaleCrawler().run()


if __name__ == "__main__":
    main()
