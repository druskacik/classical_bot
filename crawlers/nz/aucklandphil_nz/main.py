import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Auckland Philharmonia"
SOURCE_URL = "https://aucklandphil.nz/"
API_URL = f"{SOURCE_URL}wp-json/wp/v2/concert"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131.0 Safari/537.36"
)


def _session():
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


def _clean_text(node):
    if node is None:
        return None
    text = node.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text or None


def _unique_text(nodes):
    values = []
    for node in nodes:
        value = _clean_text(node)
        if value and value not in values:
            values.append(value)
    return values


def _fetch_api_posts():
    session = _session()
    posts = []
    page = 1
    while True:
        response = session.get(API_URL, params={"per_page": 100, "page": page}, timeout=45)
        response.raise_for_status()
        batch = response.json()
        posts.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
    return posts


def _parse_detail(post):
    url = post["link"]
    response = _session().get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    date_parts = _unique_text(soup.select(".concert-date-text"))
    date_text = next(
        (value for value in date_parts if re.fullmatch(r"\d{1,2} [A-Za-z]+ \d{4}", value)),
        None,
    )
    if date_text is None:
        return None
    try:
        concert_date = datetime.strptime(date_text, "%d %B %Y").date().isoformat()
    except ValueError:
        try:
            concert_date = datetime.strptime(date_text, "%d %b %Y").date().isoformat()
        except ValueError:
            return None

    time_from = next(
        (value for value in date_parts if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)),
        None,
    )
    # The first large text block holds performers and repertoire. The separate
    # concert-content block holds the long editorial description.
    content = soup.select_one(".concert-content")
    description_parts = []
    for node in soup.select(".ct-text-block.large"):
        if content is not None and (node is content or content in node.descendants):
            continue
        text = _clean_text(node)
        if text and not text.lower().startswith(("save at least", "useful information")):
            description_parts.append(text)
            break
    content_text = _clean_text(content)
    if content_text and content_text not in description_parts:
        description_parts.append(content_text)
    if not description_parts:
        fallback = BeautifulSoup(post.get("content", {}).get("rendered", ""), "html.parser")
        fallback_text = _clean_text(fallback)
        if fallback_text:
            description_parts.append(fallback_text)

    venues = _unique_text(soup.select(".concert-venue"))
    venue = venues[0] if venues else None
    if not venue:
        detail_text = " ".join(description_parts)
        for venue_name in ("Auckland Town Hall", "Q Theatre"):
            if venue_name.casefold() in detail_text.casefold():
                venue = venue_name
                break
    if not venue:
        return None

    title = html.unescape(post.get("title", {}).get("rendered", "")).strip()
    if not title:
        return None
    return {
        "title": title,
        "date": concert_date,
        "url": url,
        "time_from": time_from,
        "venue": venue,
        "city": "Auckland",
        "description": "\n\n".join(description_parts) or None,
    }


class AucklandPhilCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="aucklandphil_nz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NZ",
        upload_target="classical",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self):
        posts = _fetch_api_posts()
        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(_parse_detail, post): post["link"] for post in posts}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except requests.RequestException as error:
                    log_message(
                        "Concert detail fetch failed",
                        event="crawler_url_fetch_failed",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        records.sort(key=lambda record: (record["date"], record["time_from"] or "", record["url"]))
        return records


def main():
    AucklandPhilCrawler().run()


if __name__ == "__main__":
    main()
