import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Mariko Senju Official Site"
SOURCE_URL = "https://marikosenju.com/"
API_URL = urljoin(SOURCE_URL, "wp-json/wp/v2/concert")
FIRST_ARCHIVE_YEAR = 2014

# These names unambiguously identify municipalities in the site's venue text.
# Prefecture names alone are deliberately not treated as cities.
CITY_NAMES = (
    "札幌", "函館", "青森", "八戸", "盛岡", "仙台", "秋田", "山形", "福島",
    "郡山", "宇都宮", "高崎", "前橋", "さいたま", "川越", "所沢", "千葉",
    "東京", "八王子", "横浜", "川崎", "相模原", "新潟", "富山", "金沢",
    "福井", "甲府", "長野", "松本", "岐阜", "静岡", "浜松", "名古屋",
    "津", "大津", "京都", "大阪", "堺", "神戸", "奈良", "和歌山",
    "鳥取", "米子", "松江", "岡山", "広島", "山口", "下関", "徳島",
    "高松", "松山", "高知", "福岡", "北九州", "佐賀", "長崎", "熊本",
    "大分", "宮崎", "鹿児島", "那覇",
)


def _clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _field_groups(dl):
    """Split malformed legacy lists that contain several events in one dl."""
    groups = []
    result = {}
    for term in dl.find_all("dt"):
        value = term.find_next_sibling("dd")
        if value is not None:
            key = _clean(term.get_text(" ", strip=True))
            if key == "日時" and result:
                groups.append(result)
                result = {}
            result[key] = _clean(value.get_text("\n", strip=True))
    if result:
        groups.append(result)
    return groups


def _event_dates(value, year):
    """Return every concrete calendar date advertised in a date field."""
    value = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    explicit_year = re.search(r"(20\d{2})年", value)
    event_year = int(explicit_year.group(1)) if explicit_year else year
    month_day = re.search(r"(\d{1,2})\s*(?:月|/)\s*(\d{1,2})", value)
    if not month_day:
        return []
    month, first_day = map(int, month_day.groups())
    days = [first_day]

    # Handles compact forms such as 9/26,27 and 4月1日〜4日.
    tail = value[month_day.end():]
    second = re.search(r"(?:[、,，〜～~]|から)\s*(\d{1,2})\s*日?", tail)
    if second:
        last_day = int(second.group(1))
        if "〜" in second.group(0) or "～" in second.group(0) or "~" in second.group(0) or "から" in second.group(0):
            days = list(range(first_day, last_day + 1))
        else:
            days.append(last_day)

    parsed = []
    for day in days:
        try:
            parsed.append(date(event_year, month, day).isoformat())
        except ValueError:
            log_message(
                "Skipping invalid event date",
                event="crawler_invalid_date",
                date_text=value,
            )
    return parsed


def _city_from_venue(venue):
    special_venues = {"麻生市民館": "川崎", "軽井沢": "軽井沢", "高岡": "高岡"}
    for marker, city in special_venues.items():
        if marker in venue:
            return city
    leading = venue.split(maxsplit=1)[0] if venue.split() else ""
    if leading in {"東京", "大阪", "京都"}:
        return leading
    remainder = re.sub(
        r"^(?:北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本|大分|宮崎|鹿児島|沖縄)(?:県|府|都)?\s+",
        "",
        venue,
    )
    for city in CITY_NAMES:
        if city in remainder:
            return city
    # Municipality suffixes are the strongest signal (佐賀市文化会館, etc.).
    match = re.search(r"([一-龥ぁ-んァ-ヶー]{2,8}?市)(?:民|立|文化|会館|公会堂|\s|$)", remainder)
    if match:
        return match.group(1).removesuffix("市")
    match = re.search(r"([一-龥ぁ-んァ-ヶー]{2,8}?区)(?:民|立|文化|会館|\s|$)", remainder)
    if match:
        return match.group(1).removesuffix("区")
    return None


def _start_time(value):
    if not value:
        return None
    match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", value)
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else None


def _records_from_page(html, url, fallback_year):
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for dl in soup.select("article dl"):
        for fields in _field_groups(dl):
            date_text = fields.get("日時")
            if not date_text:
                heading = soup.select_one("article h2")
                date_text = _clean(heading.get_text(" ", strip=True)) if heading else None
            venue = fields.get("会場")
            if not date_text or not venue:
                continue
            city = _city_from_venue(venue)
            if not city:
                log_message(
                    "Skipping event without a defensible city",
                    event="crawler_event_skipped",
                    url=url,
                    venue=venue,
                )
                continue

            programme = fields.get("曲目")
            collaborators = fields.get("共演者") or fields.get("共演") or fields.get("演奏")
            description_parts = []
            if collaborators:
                description_parts.append(f"共演: {collaborators}")
            if programme:
                description_parts.append(f"曲目: {programme}")
            description = "\n".join(description_parts) or None
            title = f"千住真理子 公演 — {venue}"
            for event_date in _event_dates(date_text, fallback_year):
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": _start_time(fields.get("開演")),
                    "venue": venue,
                    "city": city,
                    "description": description,
                })
    return records


class MarikoSenjuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="marikosenju_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="JP",
        upload_target="potential",
        dedupe_subset=["date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ClassicalBot/1.0 (+concert indexer)"})

    def _get(self, url, **kwargs):
        log_message("Fetching concert source", event="crawler_url_fetch", url=url)
        response = self.session.get(url, timeout=30, **kwargs)
        response.raise_for_status()
        return response

    def _api_posts(self):
        page = 1
        while True:
            response = self._get(API_URL, params={"per_page": 100, "page": page})
            posts = response.json()
            if not posts:
                break
            yield from posts
            total_pages = int(response.headers.get("X-WP-TotalPages", page))
            if page >= total_pages:
                break
            page += 1

    def scrape(self):
        records = []

        # REST pagination supplies canonical detail URLs for current/future posts.
        for post in self._api_posts():
            url = post["link"]
            html = self._get(url).text
            slug_year = int(post["slug"][:4]) if re.match(r"20\d{2}", post["slug"]) else date.today().year
            records.extend(_records_from_page(html, url, slug_year))

        # The site retains complete annual HTML archives, including posts no
        # longer returned by the REST collection.
        for year in range(FIRST_ARCHIVE_YEAR, date.today().year + 1):
            url = urljoin(SOURCE_URL, f"concert/{year}/")
            records.extend(_records_from_page(self._get(url).text, url, year))

        log_message(
            "Parsed concert records",
            event="crawler_records_parsed",
            record_count=len(records),
        )
        return records


def main():
    MarikoSenjuCrawler().run()


if __name__ == "__main__":
    main()
