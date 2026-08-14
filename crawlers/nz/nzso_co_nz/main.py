import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "New Zealand Symphony Orchestra"
SOURCE_URL = "https://www.nzso.co.nz/"
ALGOLIA_URL = "https://srf7s95mal-dsn.algolia.net/1/indexes/entries_date_asc/query"
ALGOLIA_HEADERS = {
    "x-algolia-application-id": "SRF7S95MAL",
    "x-algolia-api-key": "d9317c556e68922642127a88488854c9",
}
EVENT_FILTER = "collection_handle:events AND published:true AND private:false"
REQUEST_TIMEOUT = 45
USER_AGENT = "Mozilla/5.0 (compatible; classical-concert-crawler/1.0)"


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _build_id(session: requests.Session) -> str:
    response = session.get(SOURCE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.find("script", id="__NEXT_DATA__")
    if node is None or not node.string:
        raise ValueError("NZSO page does not contain Next.js build metadata")
    build_id = json.loads(node.string).get("buildId")
    if not build_id:
        raise ValueError("NZSO Next.js build ID is missing")
    return build_id


def _event_hits(session: requests.Session) -> list[dict]:
    hits = []
    page = 0
    while True:
        response = session.post(
            ALGOLIA_URL,
            headers=ALGOLIA_HEADERS,
            json={
                "query": "",
                "filters": EVENT_FILTER,
                "hitsPerPage": 100,
                "page": page,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        hits.extend(payload.get("hits", []))
        if page + 1 >= int(payload.get("nbPages", 0)):
            break
        page += 1
    return hits


def _plain_text(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def _description(entry: dict) -> str | None:
    parts = []
    for value in (entry.get("rich_description"), entry.get("description")):
        text = _plain_text(value)
        if text and text not in parts:
            parts.append(text)
    for section in entry.get("sections") or []:
        for block in section.get("wysiwyg") or []:
            text = _plain_text(block.get("text"))
            if text and text not in parts:
                parts.append(text)
        text = _plain_text(section.get("section_intro"))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts) or None


def _detail_url(build_id: str, uri: str) -> str:
    return f"{SOURCE_URL}_next/data/{build_id}{uri}.json"


def _parse_entry(entry: dict, public_url: str) -> list[dict]:
    title = (entry.get("title") or "").strip()
    description = _description(entry)
    records = []
    for concert in entry.get("concerts") or []:
        venue_data = concert.get("venue") or {}
        location = venue_data.get("location") or {}
        venue = (venue_data.get("title") or "").strip()
        city = (location.get("title") or "").split("|", 1)[0].strip()
        raw_date = concert.get("date")
        if not title or not venue or not city or not raw_date:
            continue
        try:
            occurrence = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        records.append(
            {
                "title": title,
                "date": occurrence.date().isoformat(),
                "url": public_url,
                "time_from": occurrence.strftime("%H:%M"),
                "venue": venue,
                "city": city,
                "description": description,
            }
        )
    return records


class NzsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="nzso_co_nz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NZ",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from", "venue"],
    )

    def scrape(self) -> list[dict]:
        session = _session()
        build_id = _build_id(session)
        records = []
        for hit in _event_hits(session):
            uri = hit.get("uri") or hit.get("url")
            if not isinstance(uri, str) or not uri.startswith("/"):
                continue
            url = SOURCE_URL.rstrip("/") + uri
            try:
                response = session.get(_detail_url(build_id, uri), timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                entry = response.json().get("pageProps", {}).get("entry", {})
                records.extend(_parse_entry(entry, url))
            except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                log_message(
                    "NZSO event detail fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        records.sort(key=lambda item: (item["date"], item["time_from"] or "", item["url"]))
        return records


def main():
    NzsoCrawler().run()


if __name__ == "__main__":
    main()
