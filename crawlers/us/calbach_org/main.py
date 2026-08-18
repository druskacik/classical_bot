import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.calbach.org/'
SOURCE = 'California Bach Society'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CONCERT_MARKER = re.compile(r'^(?:First|Second|Third|Fourth) Concert$', re.I)
OCCURRENCE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+20\d{2}),\s*\*?'
    r'(\d{1,2}(?::\d{2})?\s*[ap]m)\*?$',
    re.I,
)
CITY_ALIASES = {
    'sf': 'San Francisco',
    'san francisco': 'San Francisco',
    'palo alto': 'Palo Alto',
    'berkeley': 'Berkeley',
}


def clean_lines(soup):
    lines = []
    for value in soup.get_text('\n', strip=True).splitlines():
        value = re.sub(r'\s+', ' ', value.replace('\xa0', ' ')).strip()
        value = value.strip('*').strip()
        if value and value not in {'—', '-'}:
            lines.append(value)
    return lines


def season_urls(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.text, 'xml')
    urls = []
    for location in sitemap.select('loc'):
        url = location.get_text(strip=True)
        path = url.removeprefix(SOURCE_URL).strip('/').lower()
        if path == 'season' or path.startswith('archive-season-'):
            urls.append(url)
    return sorted(set(urls), key=lambda url: (url != urljoin(SOURCE_URL, 'season'), url))


def parse_time(value):
    return datetime.strptime(value.replace(' ', '').upper(), '%I:%M%p').strftime('%H:%M') \
        if ':' in value else datetime.strptime(value.replace(' ', '').upper(), '%I%p').strftime('%H:%M')


def parse_location(value):
    normalized = re.sub(r'^Venue Change\s*', '', value, flags=re.I).strip(' —')
    lower = normalized.lower()
    city = next((canonical for alias, canonical in CITY_ALIASES.items()
                 if re.search(rf'(?:,|\b)\s*{re.escape(alias)}\s*$', lower)), None)
    if not city:
        return None
    venue = normalized.rsplit(',', 1)[0].strip()
    # A city-only listing announces the location but not a venue. Do not invent
    # a hall: the source varies its churches between concert sets.
    if not venue or venue.lower() in CITY_ALIASES:
        return None
    venue = re.sub(r',?\s+\d+[\w -]*\s+.+$', '', venue).strip(' ,')
    return (venue, city) if venue else None


def parse_section(lines, page_url):
    occurrence_indexes = [(index, OCCURRENCE.match(line)) for index, line in enumerate(lines)]
    occurrence_indexes = [(index, match) for index, match in occurrence_indexes if match]
    if not occurrence_indexes:
        return []

    first_index = occurrence_indexes[0][0]
    title_candidates = [line for line in lines[:first_index] if not re.search(r'\d', line)]
    title_candidates = [line for line in title_candidates if line.lower() not in {
        'buy tickets', 'read the program notes', 'first concert', 'second concert',
        'third concert', 'fourth concert',
    }]
    if not title_candidates:
        return []
    title = title_candidates[-2] if len(title_candidates) > 1 else title_candidates[-1]
    subtitle = title_candidates[-1] if len(title_candidates) > 1 else ''
    if subtitle and subtitle.lower() != title.lower():
        title = f'{title} — {subtitle}'

    last_occurrence = occurrence_indexes[-1][0]
    last_location = next(
        (index for index in range(last_occurrence + 1, min(last_occurrence + 4, len(lines)))
         if parse_location(lines[index])),
        last_occurrence,
    )
    description_lines = lines[last_location + 1:]
    for index, line in enumerate(description_lines):
        if (line.startswith('“I didn’t really know') or line in {'Summary Block', 'Featured'}
                or line.startswith('California Bach Society, a 501')):
            description_lines = description_lines[:index]
            break
    description_lines = [line for line in description_lines if line.lower() not in {
        'buy tickets', 'read the program notes', 'meet the soloists',
    }]
    description = '\n'.join(description_lines).strip() or None

    records = []
    for index, match in occurrence_indexes:
        location = None
        for candidate in lines[index + 1:index + 4]:
            location = parse_location(candidate)
            if location:
                break
        if not location:
            continue
        venue, city = location
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
            time_from = parse_time(match.group(2))
        except ValueError:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_season(html, page_url):
    lines = clean_lines(BeautifulSoup(html, 'html.parser'))
    markers = [index for index, line in enumerate(lines) if CONCERT_MARKER.match(line)]
    records = []
    for position, start in enumerate(markers):
        end = markers[position + 1] if position + 1 < len(markers) else len(lines)
        records.extend(parse_section(lines[start:end], page_url))
    return records


class CalbachOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='calbach_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = season_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch California Bach Society sitemap',
                event='crawler_fetch_failed', level='error', url=SITEMAP_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_season(response.text, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch California Bach Society season page',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        unique = {(record['title'], record['date'], record['time_from'], record['venue']): record
                  for record in records}
        return sorted(unique.values(), key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    CalbachOrgCrawler().run()


if __name__ == '__main__':
    main()
