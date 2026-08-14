from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Dunedin Symphony Orchestra"
SOURCE_URL = "https://www.dso.org.nz/"
EVENTS_URL = f"{SOURCE_URL}events"
LOCAL_TIMEZONE = ZoneInfo("Pacific/Auckland")
REQUEST_TIMEOUT = 30


def _page_data_url(path: str) -> str:
    path = path.strip("/")
    return f"{SOURCE_URL}page-data/{path}/page-data.json"


def _get_json(session: requests.Session, url: str) -> dict:
    log_message("Fetching DSO page data", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _portable_text(blocks: list[dict] | None) -> list[str]:
    paragraphs = []
    for block in blocks or []:
        text = "".join(
            child.get("text", "")
            for child in block.get("children", [])
            if isinstance(child, dict)
        ).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def _description(event: dict) -> str | None:
    sections = []

    repertoire = []
    for item in event.get("repertoire") or []:
        composer = (item.get("heading") or "").strip()
        work = (item.get("text") or "").strip()
        line = " — ".join(value for value in (composer, work) if value)
        if line:
            repertoire.append(line)
    if repertoire:
        sections.append("Programme:\n" + "\n".join(repertoire))

    sections.extend(_portable_text(event.get("description")))

    for item in event.get("additionalInfo") or []:
        heading = (item.get("heading") or "").strip()
        text = (item.get("text") or "").strip()
        section = "\n".join(value for value in (heading, text) if value)
        if section:
            sections.append(section)

    return "\n\n".join(sections) or None


class DsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="dso_org_nz",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="NZ",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update({"User-Agent": "ClassicalBot/1.0 (+concert crawler)"})

        listing = _get_json(session, _page_data_url("events"))
        events = listing["result"]["data"]["events"]["nodes"]
        records = []

        for summary in events:
            slug = summary.get("slug", {}).get("current")
            venue = (summary.get("where") or {}).get("title")
            if not slug or not venue:
                continue

            url = f"{EVENTS_URL}/{slug}"
            try:
                detail_data = _get_json(session, _page_data_url(f"events/{slug}"))
                event = detail_data["result"]["data"]["event"]
            except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                log_message(
                    "Could not fetch DSO event detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                event = summary

            title = (event.get("title") or summary.get("title") or "").strip()
            venue = ((event.get("where") or {}).get("title") or venue).strip()
            if not title or not venue:
                continue

            description = _description(event)
            for session_start in event.get("session") or summary.get("session") or []:
                try:
                    starts_at = datetime.fromisoformat(
                        session_start.replace("Z", "+00:00")
                    ).astimezone(LOCAL_TIMEZONE)
                except (AttributeError, TypeError, ValueError):
                    continue

                records.append(
                    {
                        "title": title,
                        "date": starts_at.date().isoformat(),
                        "url": url,
                        "time_from": starts_at.time().replace(tzinfo=None).isoformat(timespec="minutes"),
                        "time_to": None,
                        "venue": venue,
                        "city": "Dunedin",
                        "description": description,
                    }
                )

        return records


def main():
    DsoCrawler().run()


if __name__ == "__main__":
    main()
