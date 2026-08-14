import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "NZ Barok"
SOURCE_URL = "https://www.nzbarok.org.nz/"
API_BASE = f"{SOURCE_URL}wp-json/wp/v2"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131.0 Safari/537.36"
)
DATE_RE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+)?"
    r"\d{1,2}(?:st|nd|rd|th)?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}",
    re.IGNORECASE,
)
TIME_RE = re.compile(r"\b(\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm))\b", re.IGNORECASE)
CITY_NAMES = (
    "Auckland", "Tauranga", "Hamilton", "Wellington", "Christchurch", "Taupo",
    "Matamata", "Kaitaia", "Kerikeri", "Waihi", "Howick", "Henderson",
    "Takapuna", "Remuera", "Otara", "Manurewa", "Pakuranga", "Mt Eden",
)
VENUE_RE = re.compile(
    r"(?:at\s+)?((?:St\.?\s+)?[A-Z][^.;“”]{0,70}?"
    r"(?:Church|Chapel|Cathedral|Hall|Theatre|Gallery|Centre|Center|Estate|"
    r"Alberton|GAPA|Te Tuhi|OMAC|School))\b"
)
AUCKLAND_SUBURBS = {
    "Howick", "Henderson", "Takapuna", "Remuera", "Otara", "Manurewa",
    "Pakuranga", "Mt Eden",
}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _clean(node) -> str | None:
    if node is None:
        return None
    value = node.get_text("\n", strip=True) if hasattr(node, "get_text") else str(node)
    value = re.sub(r"[ \t]+", " ", html.unescape(value))
    value = re.sub(r"\n{2,}", "\n", value).strip()
    return value or None


def _fetch_posts(session: requests.Session, post_type: str) -> list[dict]:
    posts = []
    page = 1
    while True:
        url = f"{API_BASE}/{post_type}"
        response = session.get(url, params={"per_page": 100, "page": page}, timeout=45)
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return posts


def _parse_date(value: str, formats: tuple[str, ...]) -> str | None:
    value = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+", "", value, flags=re.IGNORECASE)
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _parse_time(value: str | None) -> str | None:
    if not value:
        return None
    match = TIME_RE.search(value)
    if not match:
        return None
    normalized = match.group(1).replace(".", ":").replace(" ", "").upper()
    if ":" not in normalized:
        normalized = re.sub(r"(?=[AP]M$)", ":00", normalized)
    try:
        return datetime.strptime(normalized, "%I:%M%p").strftime("%H:%M")
    except ValueError:
        return None


def _city(text: str) -> str | None:
    for name in CITY_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return "Auckland" if name in AUCKLAND_SUBURBS else name
    return None


def _venue(location: str) -> str | None:
    value = re.sub(r"^Location\s*", "", location, flags=re.IGNORECASE).strip()
    first = value.split("\n", 1)[0].split(",", 1)[0].strip(" ,-–")
    return first if first and first.casefold() not in {name.casefold() for name in CITY_NAMES} else None


def _parse_mec_event(session: requests.Session, post: dict) -> dict | None:
    url = post["link"]
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    date_text = _clean(soup.select_one(".mec-start-date-label"))
    location = _clean(soup.select_one(".mec-single-event-location"))
    title = html.unescape(post.get("title", {}).get("rendered", "")).strip()
    if not date_text or not location or not title:
        return None
    date = _parse_date(date_text, ("%b %d %Y", "%B %d %Y"))
    city = _city(location)
    venue = _venue(location)
    if not date or not city or not venue:
        return None

    description = _clean(soup.select_one(".mec-single-event-description"))
    if not description:
        description = _clean(BeautifulSoup(post.get("content", {}).get("rendered", ""), "html.parser"))
    return {
        "title": title,
        "date": date,
        "url": url,
        "time_from": _parse_time(_clean(soup.select_one(".mec-single-event-time abbr"))),
        "venue": venue,
        "city": city,
        "description": description,
    }


def _archive_occurrences(post: dict) -> list[dict]:
    soup = BeautifulSoup(post.get("content", {}).get("rendered", ""), "html.parser")
    description = _clean(soup)
    if not description:
        return []
    flat = " ".join(description.split())
    matches = list(DATE_RE.finditer(flat))
    records = []
    for index, match in enumerate(matches):
        date = _parse_date(match.group(), ("%d %B %Y", "%d %b %Y"))
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(flat), match.end() + 260)
        context = flat[match.start():end]
        city = _city(context) or _city(html.unescape(post["title"]["rendered"]))
        time_from = _parse_time(context)
        venue_match = VENUE_RE.search(context)
        venue = venue_match.group(1).strip(" ,-–") if venue_match else None
        if venue and (
            DATE_RE.search(venue)
            or TIME_RE.search(venue)
            or re.match(r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2},", venue)
        ) and "," in venue:
            venue = venue.rsplit(",", 1)[-1].strip()
        if venue and venue.count("(") > venue.count(")"):
            venue += ")"
        if not date or not time_from or not city or not venue:
            continue
        records.append({
            "title": html.unescape(post["title"]["rendered"]).strip(),
            "date": date,
            "url": post["link"],
            "time_from": time_from,
            "venue": venue,
            "city": city,
            "description": description,
        })
    return records


class NZBarokCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nzbarok_org_nz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NZ",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        records = []
        for post in _fetch_posts(session, "mec-events"):
            try:
                record = _parse_mec_event(session, post)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    "NZ Barok event fetch failed",
                    event="crawler_url_fetch_failed",
                    url=post.get("link"),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        for post in _fetch_posts(session, "archived-events"):
            records.extend(_archive_occurrences(post))
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["url"]))
        return records


def main():
    NZBarokCrawler().run()


if __name__ == "__main__":
    main()
