import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sites.google.com/mclean-symphony.org/home'
SOURCE = 'The McLean Symphony'
CONCERTS_URL = f'{SOURCE_URL}/concerts'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}
DATE_RE = re.compile(
    r'^(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})\s*[–—-]\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b',
    re.I,
)
NON_CONTENT_RE = re.compile(
    r'^(?:SEASON\s+\d+|Past concerts from Season|Programs and artists subject to change)',
    re.I,
)


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u202f', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date_time(value):
    match = DATE_RE.match(clean_text(value))
    if not match:
        return None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        if match.group(6).upper() == 'PM' and hour != 12:
            hour += 12
        elif match.group(6).upper() == 'AM' and hour == 12:
            hour = 0
    except ValueError:
        return None
    return event_date, f'{hour:02d}:{minute:02d}'


def parse_location(value):
    parts = [clean_text(part) for part in value.split(',')]
    if len(parts) < 3 or parts[-1].upper() != 'VA':
        return None
    venue = ', '.join(parts[:-2]).strip()
    city = parts[-2]
    if not venue or not city:
        return None
    return venue, city


def page_lines(html):
    soup = BeautifulSoup(html, 'html.parser')
    raw_lines = []
    for paragraph in soup.select('p.zfr3Q'):
        for value in paragraph.get_text('\n', strip=True).splitlines():
            text = clean_text(value)
            if text and (not raw_lines or text != raw_lines[-1]):
                raw_lines.append(text)
    lines = []
    index = 0
    while index < len(raw_lines):
        if (
            index + 3 < len(raw_lines)
            and re.match(r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),', raw_lines[index])
            and raw_lines[index + 1] in {'–', '—', '-'}
            and re.fullmatch(r'\d{1,2}:\d{2}', raw_lines[index + 2])
            and raw_lines[index + 3].upper() in {'AM', 'PM'}
        ):
            lines.append(' '.join(raw_lines[index:index + 4]))
            index += 4
            continue
        lines.append(raw_lines[index])
        index += 1
    return lines


def parse_season(html, url):
    lines = page_lines(html)
    date_indexes = [index for index, line in enumerate(lines) if parse_date_time(line)]
    if not date_indexes:
        return []

    first_index = date_indexes[0]
    prior = lines[first_index - 1] if first_index else ''
    date_before_title = not prior or bool(NON_CONTENT_RE.match(prior))
    records = []

    for position, date_index in enumerate(date_indexes):
        next_date = date_indexes[position + 1] if position + 1 < len(date_indexes) else len(lines)
        if date_before_title:
            date_match = DATE_RE.match(lines[date_index])
            inline_title = clean_text(lines[date_index][date_match.end():])
            if inline_title:
                title = inline_title
                location_index = date_index + 1
                description_start = date_index + 2
            else:
                title = lines[date_index + 1] if date_index + 1 < len(lines) else ''
                location_index = date_index + 2
                description_start = date_index + 3
        else:
            title_index = date_index - 1
            title = lines[title_index]
            location_index = date_index + 1
            description_start = date_index + 2

        if location_index >= len(lines):
            continue
        location = parse_location(lines[location_index])
        parsed_datetime = parse_date_time(lines[date_index])
        if not title or not location or not parsed_datetime:
            continue

        description_end = next_date - (0 if date_before_title else 1)
        description_lines = [
            line for line in lines[description_start:description_end]
            if not NON_CONTENT_RE.match(line)
        ]
        event_date, time_from = parsed_datetime
        venue, city = location
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': '\n'.join(description_lines) or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def canonical_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))


class McLeanSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mclean_symphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        season_urls = sorted({
            canonical_url(urljoin(CONCERTS_URL, anchor.get('href')))
            for anchor in soup.select('a[href]')
            if re.search(r'/concerts/(?:past-concerts/)?season-\d+/?$', anchor.get('href', ''))
        })

        records = []
        for url in season_urls:
            try:
                detail = requests.get(url, headers=HEADERS, timeout=45)
                detail.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape McLean Symphony season',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_season(detail.text, url))

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    McLeanSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
