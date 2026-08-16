import re
from collections import deque
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operaidaho.org/'
SOURCE = 'Opera Idaho'
SEASON_URL = urljoin(SOURCE_URL, '26-27-season/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'opera-idaho-archives/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month: number for number, month in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'),
        start=1,
    )
}
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,\s*(\d{4}))?(?:\s*[•·|,-]\s*|,\s*)?'
    r'(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))?',
    re.IGNORECASE,
)
ADDRESS_RE = re.compile(r'\b(?:Boise|Nampa|Meridian|Caldwell|Garden City|Eagle)\b', re.I)
NON_EVENT_TITLES = {'Past Seasons', 'Opera Idaho Archives', '26-27 Season', '2025-2026 Season'}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    if not value:
        return None
    value = value.strip().lower().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def season_years(url, text):
    match = re.search(r'\b(20\d{2})\s*[-–]\s*(?:20)?(\d{2,4})\b', text[:1500])
    if not match:
        match = re.search(r'/(20\d{2})-(?:20)?(\d{2,4})-season/', url)
    if not match and '/opera-idaho-archives/__trashed-2/' in url:
        return 2025, 2026
    if not match and not url.startswith(ARCHIVE_URL):
        return 2026, 2027
    if not match:
        return None
    first, second = int(match.group(1)), int(match.group(2))
    if second < 100:
        second = first // 100 * 100 + second
    return first, second


def infer_year(month, explicit_year, years):
    if explicit_year:
        return int(explicit_year)
    if not years:
        return None
    return years[0] if month >= 7 else years[1]


def venue_and_city(lines, date_index):
    window = lines[date_index + 1:date_index + 9]
    for index, line in enumerate(window):
        if line.lower() == 'venue' and index + 1 < len(window):
            venue = window[index + 1]
            following = ' '.join(window[index + 2:index + 4])
            city_match = ADDRESS_RE.search(following)
            return venue, city_match.group(0).title() if city_match else 'Boise'

    # Older production pages put the hall immediately after their date lines.
    for line in window:
        if re.search(r'\b(?:Theatre|Theater|Hall|Room|Center|Church|Park|Museum)\b', line, re.I):
            city_match = ADDRESS_RE.search(' '.join(window))
            return line, city_match.group(0).title() if city_match else 'Boise'
    return None, None


def parse_page(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main, #main, #content') or soup.body
    if not main:
        return []
    lines = [clean_text(line) for line in main.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    heading = main.select_one('h1') or soup.select_one('h1')
    title = clean_text(heading) if heading else clean_text(soup.title).removesuffix(' - Opera Idaho')
    if not title or title in NON_EVENT_TITLES:
        return []

    text = '\n'.join(lines)
    years = season_years(url, text)
    description = text or None
    records = []
    seen = set()
    for index, line in enumerate(lines):
        for match in DATE_RE.finditer(line):
            month_name, day, explicit_year, time_value = match.groups()
            month = MONTHS[month_name.title()]
            year = infer_year(month, explicit_year, years)
            if not year or not time_value:
                continue
            try:
                event_date = datetime(year, month, int(day)).date().isoformat()
            except ValueError:
                continue
            venue, city = venue_and_city(lines, index)
            time_from = parse_time(time_value)
            key = (event_date, time_from, venue)
            if not venue or not city or not time_from or key in seen:
                continue
            seen.add(key)
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'description': description,
            })
    return records


def page_links(url, html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    # Elementor renders useful season/archive navigation outside the semantic
    # main element, so inspect all anchors and constrain by first-party paths.
    for anchor in soup.select('a[href]'):
        link = urljoin(url, anchor.get('href')).split('#', 1)[0]
        parsed = urlparse(link)
        if parsed.netloc != 'operaidaho.org' or not parsed.path.endswith('/'):
            continue
        if url == SEASON_URL and parsed.path.count('/') == 2:
            links.add(link)
        elif url.startswith(ARCHIVE_URL) and link.startswith(ARCHIVE_URL):
            links.add(link)
    return links


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    queue = deque([(SEASON_URL, 0), (ARCHIVE_URL, 0)])
    visited = set()
    records = []

    while queue and len(visited) < 250:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Opera Idaho page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        records.extend(parse_page(response.url, response.text))
        if depth < 2:
            queue.extend((link, depth + 1) for link in sorted(page_links(response.url, response.text)))

    if not records:
        log_message(
            'No concrete Opera Idaho performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class OperaIdahoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaidaho_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    OperaIdahoOrgCrawler().run()


if __name__ == '__main__':
    main()
