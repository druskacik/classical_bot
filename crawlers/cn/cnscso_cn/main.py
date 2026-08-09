import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.cnscso.cn/"
SOURCE = "四川交响乐团"
LIST_URL = urljoin(SOURCE_URL, "news.aspx?mid=93")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    )
}


def _get(url):
    last_error = None
    for _ in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
    raise last_error


def _clean_text(element):
    if element is None:
        return ""
    return re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()


def _calendar_dates(text):
    """Extract all performance dates, including compact Chinese date ranges."""
    text = text.replace("–", "-").replace("—", "-")
    first = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", text)
    if not first:
        return []

    year, month, day = map(int, first.groups())
    values = []

    def add(year_value, month_value, day_value):
        try:
            value = date(year_value, month_value, day_value)
        except ValueError:
            return
        if value not in values:
            values.append(value)

    add(year, month, day)
    tail = text[first.end():]

    # A second month is explicit in forms such as 5月31日、6月1日.
    for match in re.finditer(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", tail):
        add(year, int(match.group(1)), int(match.group(2)))

    # Same-month ranges/lists: 7月3日-4日 and 12月24、25日.
    short = re.match(r"\s*[-、,，至到]\s*(\d{1,2})\s*日", tail)
    if short:
        final_day = int(short.group(1))
        separator = tail[:short.end()]
        if "-" in separator or "至" in separator or "到" in separator:
            try:
                start = date(year, month, day)
                end = date(year, month, final_day)
                if 0 < (end - start).days <= 31:
                    current = start + timedelta(days=1)
                    while current <= end:
                        add(current.year, current.month, current.day)
                        current += timedelta(days=1)
            except ValueError:
                pass
        else:
            add(year, month, final_day)

    return sorted(values)


def _times(text):
    normalized = text.replace("：", ":")
    results = []
    for match in re.finditer(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)", normalized):
        hour = int(match.group(1))
        if "晚" in normalized[max(0, match.start() - 3):match.start()] and hour < 12:
            hour += 12
        if hour < 24:
            value = f"{hour:02d}:{match.group(2)}"
            if value not in results:
                results.append(value)
    return results


def _city_for_venue(venue):
    if "国家大剧院" in venue:
        return "北京"
    if "贵阳" in venue:
        return "贵阳"
    # All remaining named halls in this orchestra's calendar are in Chengdu.
    return "成都"


def _parse_listing_page(html):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for node in soup.select(".pfest-list > li"):
        title_node = node.select_one("h3.t a")
        venue_node = node.select_one(".add")
        time_node = node.select_one(".c")
        if not title_node or not venue_node or not time_node:
            continue
        title = _clean_text(title_node)
        venue = re.sub(r"^[^\w\u4e00-\u9fff]+", "", _clean_text(venue_node)).strip()
        schedule = _clean_text(time_node)
        dates = _calendar_dates(schedule)
        if not title or not venue or venue == "城市巡演" or not dates:
            continue
        times = _times(schedule)
        url = urljoin(SOURCE_URL, title_node.get("href", ""))
        if not url:
            continue
        for index, performance_date in enumerate(dates):
            time_from = times[index] if len(times) == len(dates) else (times[0] if times else None)
            items.append({
                "title": title,
                "date": performance_date.isoformat(),
                "url": url,
                "time_from": time_from,
                "time_to": None,
                "venue": venue,
                "city": _city_for_venue(venue),
            })
    return items, soup


def _description(url):
    try:
        soup = BeautifulSoup(_get(url).content, "html.parser")
        text = _clean_text(soup.select_one(".npd-item"))
        return text or None
    except requests.RequestException as error:
        log_message(
            "Concert detail fetch failed",
            event="crawler_url_fetch_failed",
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None


class CnscsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="cnscso_cn",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="CN",
        upload_target="classical",
        dedupe_subset=["title", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        first_response = _get(LIST_URL)
        records, soup = _parse_listing_page(first_response.content)
        page_numbers = []
        for link in soup.select('a[href*="page="]'):
            match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
            if match:
                page_numbers.append(int(match.group(1)))
        last_page = max(page_numbers, default=1)

        for page in range(2, last_page + 1):
            url = f"{LIST_URL}&page={page}"
            try:
                page_records, _ = _parse_listing_page(_get(url).content)
                records.extend(page_records)
            except requests.RequestException as error:
                log_message(
                    "Concert listing page fetch failed",
                    event="crawler_url_fetch_failed",
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        descriptions = {}
        urls = sorted({record["url"] for record in records})
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_description, url): url for url in urls}
            for future in as_completed(futures):
                descriptions[futures[future]] = future.result()

        for record in records:
            record["description"] = descriptions.get(record["url"])

        log_message(
            "Concert catalogue parsed",
            event="crawler_catalogue_parsed",
            record_count=len(records),
        )
        return records


def main():
    CnscsoCrawler().run()


if __name__ == "__main__":
    main()
