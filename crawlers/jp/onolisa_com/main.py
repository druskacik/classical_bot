import re
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://onolisa.com/"
SOURCE = "Lisa Ono Official Website"
SITEMAP_URL = "https://onolisa.com/sitemap.xml"
HEADERS = {"User-Agent": "classical-bot/1.0 (+https://github.com/)"}

# The site normally supplies only a prefecture plus a venue name. These
# first-party place names let us retain events where the municipality is
# nevertheless unambiguous. Unknown locations are deliberately skipped.
PLACE_CITIES = {
    "東京": "Tokyo",
    "町田": "Machida",
    "関内": "Yokohama",
    "千代田": "Chiyoda",
    "五所川原": "Goshogawara",
    "八ヶ岳": "Minamimaki",
    "鳥羽": "Toba",
    "入間": "Iruma",
    "南砺": "Nanto",
    "なんと": "Nanto",
    "砺波": "Tonami",
    "湯沢": "Yuzawa",
    "秋田": "Akita",
    "草加": "Soka",
    "常陸太田": "Hitachiota",
    "大阪": "Osaka",
    "仙台": "Sendai",
    "名古屋": "Nagoya",
    "福岡": "Fukuoka",
    "博多": "Fukuoka",
    "鎌倉": "Kamakura",
    "幕張": "Chiba",
    "流山": "Nagareyama",
    "逗子": "Zushi",
    "クアラルンプール": "Kuala Lumpur",
    "香港": "Hong Kong",
    "シンガポール": "Singapore",
    "台北": "Taipei",
    "高雄": "Kaohsiung",
    "Brisbane": "Brisbane",
    "Melbourne": "Melbourne",
    "Sydney": "Sydney",
    "Adelaide": "Adelaide",
}
AUSTRALIAN_CITIES = {"Brisbane", "Melbourne", "Sydney", "Adelaide"}
COUNTRY_BY_CITY = {
    **{city: "AU" for city in AUSTRALIAN_CITIES},
    "Kuala Lumpur": "MY",
    "Hong Kong": "HK",
    "Singapore": "SG",
    "Taipei": "TW",
    "Kaohsiung": "TW",
}
VENUE_FALLBACKS = {
    "Lisa-Ono-with-Febian-Reza-Pane-Duo-Delights-2026": "常陸太田市民交流センター パルティホール",
    "toninho-horta-with-guest-lisa2026": "Billboard Live Osaka",
}


def _get(session: requests.Session, url: str) -> requests.Response:
    log_message("Fetching crawler URL", event="crawler_url_fetch", url=url)
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _field_map(article: BeautifulSoup) -> dict[str, str]:
    fields = {}
    for term in article.select("dt"):
        value = term.find_next_sibling("dd")
        if value:
            fields[_clean(term.get_text(" ", strip=True))] = _clean(
                value.get_text("\n", strip=True)
            )
    return fields


def _parse_date(value: str) -> str | None:
    match = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", value)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


def _parse_start_time(value: str) -> str | None:
    normalized = value.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))
    patterns = (
        r"(?:開演|start)\s*(\d{1,2})\s*[:：時]\s*(\d{2})?",
        r"(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:開演|[～〜~-])",
        r"(?:^|\s)(\d{1,2})\s*[:：]\s*(\d{2})(?:\s|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if hour < 24 and minute < 60:
                return f"{hour:02d}:{minute:02d}"
    return None


def _city(title: str, venue: str) -> str | None:
    evidence = f"{venue} {title}"
    # Prefer more specific place names over broad names such as Tokyo.
    for place in sorted(PLACE_CITIES, key=len, reverse=True):
        if place in evidence:
            return PLACE_CITIES[place]
    return None


def _venue(value: str) -> str:
    value = re.sub(r"〒\s*\d{3}-?\d{4}.*$", "", value).strip()
    value = re.sub(r"(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県)[^｜|]*?(?:市|区|町|村)[^｜|]*$", "", value).strip()
    return _clean(value.strip(" |｜"))


def _description(article: BeautifulSoup) -> str | None:
    parts = []
    for term in article.select("dt"):
        label = _clean(term.get_text(" ", strip=True))
        if "チケット" in label or "料金" in label or "問い合わせ" in label:
            continue
        value = term.find_next_sibling("dd")
        if value:
            text = _clean(value.get_text("\n", strip=True))
            if text:
                parts.append(f"{label}: {text}")
    return "\n".join(parts) or None


class OnolisaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="onolisa_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="JP",
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["url", "date", "time_from"],
    )

    def scrape(self) -> list[dict]:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=4,
                    backoff_factor=0.5,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=("GET",),
                )
            ),
        )
        sitemap = BeautifulSoup(_get(session, SITEMAP_URL).content, "xml")
        urls = sorted(
            {
                loc.get_text(strip=True)
                for loc in sitemap.find_all("loc")
                if urlparse(loc.get_text(strip=True)).path.startswith("/live/")
                and not urlparse(loc.get_text(strip=True)).path.startswith("/en/live/")
            }
        )

        records = []
        for url in urls:
            try:
                soup = BeautifulSoup(_get(session, url).content, "html.parser")
                article = soup.select_one("article.live-entry")
                heading = article.find("h1") if article else None
                if not article or not heading:
                    continue
                title = _clean(heading.get_text(" ", strip=True))
                fields = _field_map(article)
                date_text = fields.get("日 時", "")
                venue_text = fields.get("会 場", "") or VENUE_FALLBACKS.get(
                    urlparse(url).path.rsplit("/", 1)[-1], ""
                )
                date = _parse_date(date_text)
                venue = _venue(venue_text)
                city = _city(title, venue_text)
                if not date or not venue or not city:
                    log_message(
                        "Skipping event with incomplete location or date",
                        event="crawler_record_skipped",
                        url=url,
                        missing_date=not bool(date),
                        missing_venue=not bool(venue),
                        missing_city=not bool(city),
                    )
                    continue
                country_code = COUNTRY_BY_CITY.get(city, "JP")
                records.append(
                    {
                        "title": title,
                        "date": date,
                        "url": url,
                        "time_from": _parse_start_time(date_text),
                        "time_to": None,
                        "venue": venue,
                        "city": city,
                        "country_code": country_code,
                        "description": _description(article),
                    }
                )
            except requests.RequestException as error:
                log_message(
                    "Failed to fetch event detail",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        log_message(
            "Parsed live-event sitemap",
            event="crawler_parse_completed",
            url=SITEMAP_URL,
            discovered_count=len(urls),
            record_count=len(records),
        )
        return records


def main():
    OnolisaCrawler().run()


if __name__ == "__main__":
    main()
