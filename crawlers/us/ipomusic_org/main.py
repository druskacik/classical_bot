import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://ipomusic.org/'
SOURCE = 'Illinois Philharmonic Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(20\d{{2}})\b', re.I
)
TIME_RE = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m?\.?\b', re.I)

PAGE_SLUG_RE = re.compile(
    r'(?:^|-)season(?:-|$)|summer|chamber-night|youth-concert$', re.I
)
EXCLUDED_SLUGS = {'2026-27-season-preview', 'youth-programs'}


def clean_text(value):
    soup = BeautifulSoup(str(value or ''), 'html.parser')
    for node in soup.find_all('br'):
        node.replace_with('\n')
    for node in soup.find_all(['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li']):
        node.append('\n')
    text = soup.get_text('', strip=False)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'^\s*\[/?vc[^]]*]\s*$', '', text, flags=re.M)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(match):
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def fetch_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'slug,link,title,content',
            },
            timeout=45,
        )
        if response.status_code == 400 and page_number > 1:
            break
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        if len(batch) < 100:
            break
        page_number += 1
    return pages


def nearby_title(lines, date_index, date_match, page_title):
    prefix = lines[date_index][:date_match.start()].strip(' -–—|')
    if prefix and len(prefix) > 3 and prefix.lower() not in {
        'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
    }:
        return prefix.title() if prefix.isupper() else prefix

    candidates = []
    for index in range(max(0, date_index - 3), min(len(lines), date_index + 4)):
        if index == date_index:
            continue
        line = lines[index].strip()
        if not line or '[' in line or DATE_RE.search(line) or TIME_RE.fullmatch(line):
            continue
        if re.search(r'venue|ticket|admission|doors|conductor|season|call the|all concerts', line, re.I):
            continue
        score = 2 if line.isupper() else 0
        score += 1 if index < date_index else 0
        score -= abs(index - date_index)
        candidates.append((score, line))
    if candidates:
        title = max(candidates, key=lambda item: item[0])[1]
        return title.title() if title.isupper() else title
    return page_title


def extract_location(text):
    patterns = [
        r'Concert Venue:\s*([^\n(]+?)\s*\(([^,()]+),\s*IL\)',
        r'([^\n•]+?)\s*•\s*(?:[^\n,]+,\s*)?([^,\n]+),\s*IL(?:\s+\d{5})?',
        r'([^\n]+?)\n([^,\n]+),\s*IL\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            venue = match.group(1).strip(' .•')
            city = match.group(2).strip(' .•')
            if venue and city and venue.lower() != city.lower():
                return venue, city
    if re.search(r'\bOzinga Chapel\b', text, re.I):
        return 'Ozinga Chapel', 'Palos Heights'
    return None, None


def page_records(page):
    slug = page.get('slug', '')
    if slug in EXCLUDED_SLUGS or not PAGE_SLUG_RE.search(slug):
        return []

    html = page.get('content', {}).get('rendered', '')
    text = clean_text(html)
    lines = [line for line in text.splitlines() if line.strip()]
    page_title = clean_text(page.get('title', {}).get('rendered', ''))
    dates = [(index, DATE_RE.search(line)) for index, line in enumerate(lines)]
    dates = [(index, match) for index, match in dates if match]
    if not dates:
        return []

    global_venue, global_city = extract_location(text[:2500])
    records = []
    seen_dates = set()
    for position, (line_index, date_match) in enumerate(dates):
        start = max(0, line_index - 3)
        end = dates[position + 1][0] if position + 1 < len(dates) else len(lines)
        section = '\n'.join(lines[start:end])
        venue, city = extract_location(section[:1200])
        venue = venue or global_venue
        city = city or global_city
        event_date = parse_date(date_match)
        if event_date in seen_dates:
            continue
        seen_dates.add(event_date)
        title = nearby_title(lines, line_index, date_match, page_title)
        if title.startswith('['):
            title = page_title
        if 'chamber-night' in slug:
            title = page_title
        elif 'summer' in slug:
            for line in lines[line_index + 1:line_index + 4]:
                if line and not TIME_RE.fullmatch(line):
                    title = line
                    break
        if not all((event_date, title, venue, city)):
            continue

        times = [parse_time(' '.join(lines[line_index:line_index + 2]))]
        if 'chamber-night' in slug:
            performance = re.search(r'([^\n]+)\s*[–-]\s*IPO performs', section, re.I)
            times = [parse_time(performance.group(1))] if performance else times
        elif slug == 'youth-concert':
            times = []
            for match in TIME_RE.finditer(section):
                value = parse_time(match.group(0))
                if value and value not in times:
                    times.append(value)
            times = times or [None]

        for event_time in times:
            records.append({
                'title': title,
                'date': event_date,
                'url': page['link'],
                'time_from': event_time,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': section or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page in fetch_pages(session):
        records.extend(page_records(page))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record

    result = sorted(unique.values(), key=lambda item: (item['date'], item['title']))
    if not result:
        log_message(
            'No concert occurrences found in WordPress pages',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return result


class IpomusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ipomusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    IpomusicOrgCrawler().run()


if __name__ == '__main__':
    main()
