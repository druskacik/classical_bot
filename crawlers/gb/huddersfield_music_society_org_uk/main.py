import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://huddersfield-music-society.org.uk/'
SOURCE = 'Huddersfield Music Society'
WHATS_ON_URL = urljoin(SOURCE_URL, 'whats-on/')
SITEMAP_URL = urljoin(SOURCE_URL, 'us_portfolio-sitemap.xml')
CITY = 'Huddersfield'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', value, flags=re.IGNORECASE)
    match = re.search(
        r'(?:([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})|'
        r'(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2}))',
        value,
    )
    if not match:
        return None
    if match.group(1):
        value = f'{match.group(1)} {match.group(2)}, {match.group(3)}'
    else:
        value = f'{match.group(4)} {match.group(5)} {match.group(6)}'
    for pattern in ('%B %d, %Y', '%d %B %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(content):
    match = re.search(
        r'(?:\bTime\s*\|\s*|,\s*)'
        r'(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b',
        content,
        re.IGNORECASE,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def normalise_venue(value):
    venue = clean_text(value).replace(' | ', ', ')
    venue = re.sub(r'\s*,\s*', ', ', venue).strip(' ,')
    return venue or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    title = clean_text(soup.select_one('main h1.entry-title, main h1'))
    date_text = clean_text(soup.select_one('.concert_date .w-post-elm-value, .concert_date'))
    location_text = clean_text(
        soup.select_one('.concert_location .w-post-elm-value, .concert_location')
    )
    event_date = parse_date(date_text)
    venue = normalise_venue(location_text)
    body = soup.select_one('main .post_content')
    description = clean_text(body) or None

    # Newer entries keep date, time, and venue in the first content paragraph,
    # while older entries use dedicated WordPress custom fields.
    if body is not None and (not event_date or not venue):
        intro_lines = [line for line in clean_text(body.select_one('p')).splitlines() if line]
        if intro_lines:
            event_date = event_date or parse_date(intro_lines[0])
        if len(intro_lines) > 1:
            venue = venue or normalise_venue(intro_lines[1])

    if not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description or ''),
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def discover_event_urls(session):
    urls = []
    for index_url in (WHATS_ON_URL, SITEMAP_URL):
        response = session.get(index_url, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml' if index_url.endswith('.xml') else 'html.parser')
        if index_url.endswith('.xml'):
            candidates = [clean_text(node) for node in soup.select('url > loc')]
        else:
            candidates = [urljoin(index_url, node['href']) for node in soup.select(
                'main a[href*="/concerts/"][href]'
            )]
        urls.extend(url for url in candidates if '/concerts/' in url)
    return list(dict.fromkeys(urls))


class HuddersfieldMusicSocietyOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='huddersfield_music_society_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            urls = discover_event_urls(session)
        except requests.RequestException as error:
            log_message(
                'Failed to discover Huddersfield Music Society concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(response.content, url)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Huddersfield Music Society concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    HuddersfieldMusicSocietyOrgUkCrawler().run()


if __name__ == '__main__':
    main()
