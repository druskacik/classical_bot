import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://psarlington.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'The Philharmonic Society of Arlington'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})(?:\.?\s+at\s+'
    r'(\d{1,2}(?::\d{2})?)\s*([AP]M))?',
    re.IGNORECASE,
)
LOCATION_RE = re.compile(r'^(.+?),\s*([^,]+),\s*MA\b', re.IGNORECASE)
SEPARATOR_RE = re.compile(r'^_{10,}$')


def clean_text(value):
    value = html.unescape(str(value or '')).replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(match):
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(value, meridiem):
    if not value or not meridiem:
        return None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(f'{value} {meridiem}', pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def content_lines(rendered):
    soup = BeautifulSoup(rendered or '', 'html.parser')
    return [line for line in (clean_text(item) for item in soup.get_text('\n').splitlines()) if line]


def event_title(prefix):
    candidates = []
    for line in prefix:
        lowered = line.lower()
        if (
            'music director' in lowered
            or lowered.startswith('the arlington')
            or 'chorus' in lowered
            or 'chorale' in lowered
            or 'orchestra' in lowered
            or lowered.startswith('all programs')
            or lowered.startswith('dates are')
            or 'concert season' in lowered
        ):
            continue
        candidates.append(line)
    event_heading_indexes = [
        index for index, line in enumerate(candidates)
        if 'concert' in line.lower() or 'festival' in line.lower()
    ]
    if event_heading_indexes:
        heading_start = event_heading_indexes[-1]
        for index in reversed(event_heading_indexes[:-1]):
            if index == heading_start - 1:
                heading_start = index
            else:
                break
        chosen = candidates[heading_start:]
    else:
        chosen = candidates[-1:]
    return ' — '.join(chosen) if chosen else ''


def parse_season_page(page):
    lines = content_lines(page.get('content', {}).get('rendered'))
    date_indexes = [index for index, line in enumerate(lines) if DATE_RE.search(line)]
    records = []

    for position, date_index in enumerate(date_indexes):
        match = DATE_RE.search(lines[date_index])
        event_date = parse_date(match)
        if not event_date:
            continue

        start = date_index - 1
        while start >= 0 and not SEPARATOR_RE.match(lines[start]) and not DATE_RE.search(lines[start]):
            start -= 1
        end = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        for index in range(date_index + 1, end):
            if SEPARATOR_RE.match(lines[index]):
                end = index
                break
        block = lines[start + 1:end]

        title = event_title(lines[start + 1:date_index])
        venue = city = ''
        for line in lines[date_index + 1:end]:
            location = LOCATION_RE.search(line)
            if location:
                venue = clean_text(location.group(1))
                city = clean_text(location.group(2))
                break
        if not title or not venue or not city:
            continue

        time_from = parse_time(match.group(4), match.group(5))
        if not time_from:
            for line in block:
                time_match = re.search(
                    r'(?:concert|performance)\s+at\s+(\d{1,2}(?::\d{2})?)\s*([AP]M)',
                    line,
                    re.IGNORECASE,
                )
                if time_match:
                    time_from = parse_time(time_match.group(1), time_match.group(2))
                    break

        records.append({
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': '\n'.join(block) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(
        API_URL,
        params={
            'search': 'concert season',
            'per_page': 100,
            '_fields': 'id,slug,link,title,content',
        },
        timeout=45,
    )
    response.raise_for_status()

    records = []
    for page in response.json():
        slug = clean_text(page.get('slug'))
        title = clean_text(page.get('title', {}).get('rendered'))
        if not re.fullmatch(r'\d{4}-\d{4}-concert-season', slug):
            continue
        if 'concert season' not in title.lower():
            continue
        records.extend(parse_season_page(page))

    if not records:
        log_message(
            'No concert occurrences found in season pages',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class PsArlingtonOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='psarlington_org',
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
    PsArlingtonOrgCrawler().run()


if __name__ == '__main__':
    main()
