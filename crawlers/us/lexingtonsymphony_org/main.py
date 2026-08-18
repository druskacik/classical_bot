import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Lexington Symphony"
SOURCE_URL = "https://www.lexingtonsymphony.org/"
SEASON_URLS = (
    "https://www.lexingtonsymphony.org/20262027-season",
    "https://www.lexingtonsymphony.org/20252026-season",
)
ARCHIVE_URL = "https://www.lexingtonsymphony.org/concert-archive"
DANA_HOME_URL = "https://www.lexingtonsymphony.org/dana-home-chamber-concerts"

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"\b(?P<month>{MONTH})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4})\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[AP]M)\b", re.I)
SEASON_RE = re.compile(r"^\d{4}-\d{4}\s+SEASON$", re.I)


def _clean_lines(element):
    return [
        re.sub(r"\s+", " ", line).strip()
        for line in element.get_text("\n").splitlines()
        if line.strip()
    ]


def _date(match):
    value = f"{match.group('month')} {match.group('day')} {match.group('year')}"
    for date_format in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unparseable event date: {value}")


def _time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("ampm").upper() == "PM":
        hour += 12
    return f"{hour:02d}:{int(match.group('minute') or 0):02d}"


def _title_before(lines, line_index, date_match):
    prefix = lines[line_index][: date_match.start()].strip(" ,|-–")
    prefix = re.sub(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*", "", prefix, flags=re.I)
    prefix = re.sub(r",?\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$", "", prefix, flags=re.I)
    if prefix:
        return prefix
    for index in range(line_index - 1, -1, -1):
        candidate = lines[index].strip()
        if DATE_RE.search(candidate) or SEASON_RE.match(candidate):
            continue
        if candidate.lower().startswith(("pre-concert", "concert archive")):
            continue
        return candidate
    return ""


def _parse_page(url, venue):
    log_message("Fetching concert page", event="crawler_url_fetch", url=url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    main = soup.find("main")
    if main is None:
        raise ValueError(f"Concert page has no main content: {url}")

    lines = _clean_lines(main)
    occurrences = []
    for line_index, line in enumerate(lines):
        matches = list(DATE_RE.finditer(line))
        first_title = None
        for match_index, match in enumerate(matches):
            end = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(line)
            title = first_title or _title_before(lines, line_index, match)
            first_title = title
            suffix = line[match.end() : end]
            times = list(TIME_RE.finditer(suffix))
            for time_index, time_match in enumerate(times or [None]):
                event_title = title
                if time_match is not None:
                    context_end = times[time_index + 1].start() if time_index + 1 < len(times) else len(suffix)
                    context = suffix[time_match.end() : context_end]
                    if "KIDS POPS" in context.upper():
                        event_title = "Kids POPS!"
                    time_from = _time(time_match.group(0))
                else:
                    time_from = None
                occurrences.append(
                    {
                        "line_index": line_index,
                        "title": event_title,
                        "date": _date(match),
                        "time_from": time_from,
                    }
                )

    records = []
    for index, occurrence in enumerate(occurrences):
        if not occurrence["title"]:
            continue
        is_pops = "POPS" in occurrence["title"].upper()
        related = (
            [
                item
                for item in occurrences
                if "POPS" in item["title"].upper()
                and abs(item["line_index"] - occurrence["line_index"]) <= 3
            ]
            if is_pops
            else [occurrence]
        )
        start = min((item["line_index"] for item in related), default=occurrence["line_index"])
        end = len(lines)
        for later in occurrences[index + 1 :]:
            same_pops_block = is_pops and "POPS" in later["title"].upper() and later["line_index"] - start <= 3
            if later["title"] != occurrence["title"] and not same_pops_block:
                end = max(start + 1, later["line_index"] - 1)
                break
        description = "\n".join(lines[start:end]).strip() or None
        event_venue = (
            "Online"
            if "concert-archive" in url and "2020-09-01" <= occurrence["date"] <= "2021-05-31"
            else venue
        )
        records.append(
            {
                "title": occurrence["title"],
                "date": occurrence["date"],
                "url": url,
                "time_from": occurrence["time_from"],
                "venue": event_venue,
                "city": "Lexington",
                "description": description,
            }
        )
    return records


class LexingtonSymphonyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="lexingtonsymphony_org",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="US",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        records = []
        for url in SEASON_URLS:
            records.extend(_parse_page(url, "Cary Hall"))
        records.extend(_parse_page(ARCHIVE_URL, "Cary Hall"))
        records.extend(_parse_page(DANA_HOME_URL, "Lexington Community Center"))
        log_message(
            "Concert pages parsed",
            event="crawler_scrape_parsed",
            record_count=len(records),
        )
        return records


def main():
    LexingtonSymphonyCrawler().run()


if __name__ == "__main__":
    main()
