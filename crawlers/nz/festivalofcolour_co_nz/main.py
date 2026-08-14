import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Wānaka Festival of Colour"
SOURCE_URL = "https://festivalofcolour.co.nz/"
PROGRAMME_URLS = [urljoin(SOURCE_URL, f"{year}-programme") for year in range(2023, 2027)]
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131.0 Safari/537.36"
)
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        1,
    )
}
DATE_RE = re.compile(
    r"(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:\s+(?P<month>" + "|".join(MONTHS) + r"))?",
    re.I,
)
TIME_RE = re.compile(r"(?<!\d)(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", re.I)


def _session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _clean_text(node):
    if node is None:
        return None
    text = node.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def _field(block, labels):
    labels = {label.casefold() for label in labels}
    for row in block.select(".info-s"):
        parts = [part.get_text(" ", strip=True) for part in row.find_all(recursive=False)]
        parts = [part for part in parts if part]
        if len(parts) >= 2 and parts[0].casefold() in labels:
            return " ".join(parts[1:])
    return None


def _parse_time(value):
    match = TIME_RE.search(value or "")
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    return f"{hour:02d}:{int(match.group(2) or 0):02d}"


def _dates(value, year):
    matches = list(DATE_RE.finditer(value or ""))
    results = []
    for index, match in enumerate(matches):
        month_name = match.group("month")
        if not month_name:
            month_name = next(
                (later.group("month") for later in matches[index + 1 :] if later.group("month")),
                None,
            )
        if not month_name:
            month_name = next(
                (earlier.group("month") for earlier in reversed(matches[:index]) if earlier.group("month")),
                None,
            )
        if not month_name:
            continue
        try:
            parsed = date(year, MONTHS[month_name.lower()], int(match.group("day")))
        except ValueError:
            continue
        results.append((parsed.isoformat(), match.start(), match.end()))
    return results


def _clean_venue(value):
    if not value:
        return None
    value = re.sub(r"^Venue\s*", "", value, flags=re.I).strip(" -;,.")
    meet = re.search(r"Meet at\s+(.+?)(?:\.|$)", value, re.I)
    if meet:
        value = meet.group(1)
    return value.strip(" -;,.") or None


def _city_for(venue):
    folded = venue.casefold()
    city_markers = (
        ("queenstown", "Queenstown"),
        ("te atamira", "Queenstown"),
        ("hāwea", "Hāwea Flat"),
        ("hawea", "Hāwea Flat"),
        ("bannockburn", "Bannockburn"),
        ("cromwell", "Cromwell"),
        ("luggate", "Luggate"),
        ("arrowtown", "Arrowtown"),
        ("wānaka", "Wānaka"),
        ("wanaka", "Wānaka"),
    )
    for marker, city in city_markers:
        if marker in folded:
            return city
    # Festival performances without an explicit touring location are based in
    # Wānaka; touring venues above always override this institutional default.
    return "Wānaka"


def _occurrences(date_text, time_text, default_venue, year):
    parsed_dates = _dates(date_text, year)
    date_matches = list(DATE_RE.finditer(date_text or ""))
    time_groups = {}
    weekday_re = re.compile(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        re.I,
    )
    weekday_matches = list(weekday_re.finditer(time_text or ""))
    for index, match in enumerate(weekday_matches):
        end = weekday_matches[index + 1].start() if index + 1 < len(weekday_matches) else len(time_text)
        time_groups[match.group(1).casefold()] = [
            _parse_time(item.group(0)) for item in TIME_RE.finditer(time_text[match.end():end])
        ]
    all_times = [_parse_time(match.group(0)) for match in TIME_RE.finditer(time_text or "")]
    occurrences = []
    for index, (day, start, end) in enumerate(parsed_dates):
        next_start = parsed_dates[index + 1][1] if index + 1 < len(parsed_dates) else len(date_text)
        clause = date_text[start:next_start]
        venue = default_venue
        override = re.search(r"\s[-–]\s*(.+)$", clause)
        if override:
            candidate = re.sub(TIME_RE, "", override.group(1)).strip(" -;,.")
            if candidate:
                venue = candidate
        venue = _clean_venue(venue)
        if not venue:
            continue
        inline_time = _parse_time(clause)
        weekday = date_matches[index].group("weekday").casefold()
        times = [inline_time] if inline_time else time_groups.get(weekday, [])
        if not times:
            times = [all_times[index]] if index < len(all_times) else ([all_times[0]] if len(all_times) == 1 else [None])
        for time_from in times:
            occurrences.append((day, time_from, venue, _city_for(venue)))
    return occurrences


def _programme_links():
    session = _session()
    links = {}
    for programme_url in PROGRAMME_URLS:
        response = session.get(programme_url, timeout=45)
        response.raise_for_status()
        year = int(programme_url.rsplit("/", 1)[-1].split("-", 1)[0])
        soup = BeautifulSoup(response.content, "html.parser")
        for anchor in soup.select('a[href*="/programme/"]'):
            url = urljoin(SOURCE_URL, anchor.get("href"))
            if url.startswith(urljoin(SOURCE_URL, "programme/")):
                links[url] = year
    return links


def _parse_detail(url, programme_year):
    response = _session().get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    title_node = soup.select_one("h1")
    title = title_node.get_text(" ", strip=True) if title_node else None
    if not title:
        return []

    block = next(
        (item for item in soup.select(".categories-wise-info") if "old-event-info-layout" not in (item.get("class") or [])),
        None,
    ) or soup.select_one(".categories-wise-info")
    if block is None:
        return []
    year_text = block.get_text(" ", strip=True)
    year_match = re.search(r"\b(20\d{2})\b", year_text)
    year = int(year_match.group(1)) if year_match else programme_year
    date_text = _field(block, ("When", "Date"))
    time_text = _field(block, ("Time",))
    venue = _field(block, ("Venue",))
    if not date_text or not venue:
        return []

    description_node = soup.select_one(".sec-3-whaton-copy")
    description = _clean_text(description_node)
    records = []
    for concert_date, time_from, occurrence_venue, city in _occurrences(date_text, time_text, venue, year):
        records.append(
            {
                "title": title,
                "date": concert_date,
                "url": url,
                "time_from": time_from,
                "venue": occurrence_venue,
                "city": city,
                "description": description,
            }
        )
    return records


class FestivalOfColourCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="festivalofcolour_co_nz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NZ",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self):
        links = _programme_links()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(_parse_detail, url, year): url
                for url, year in links.items()
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except requests.RequestException as error:
                    log_message(
                        "Programme detail fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["url"]))
        return records


def main():
    FestivalOfColourCrawler().run()


if __name__ == "__main__":
    main()
