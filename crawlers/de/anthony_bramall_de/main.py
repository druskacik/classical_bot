import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://anthony-bramall.de/'
SCHEDULE_URL = f'{SOURCE_URL}termine.html'
SOURCE = 'Anthony Bramall'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

VENUE_LOCATIONS = {
    'Staatstheater am Gärtnerplatz': ('München', 'DE'),
    'Gärtnerplatztheater München': ('München', 'DE'),
    'Theater Erfurt': ('Erfurt', 'DE'),
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    value = clean_text(value)
    for date_format in ('%d.%m.%Y', '%d.%m.%y'):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return None


def parse_homepage(html):
    soup = BeautifulSoup(html, 'html.parser')
    concert_heading = next(
        (heading for heading in soup.select('.artikel h1') if heading.get_text(strip=True).startswith('Konzerte')),
        None,
    )
    details = concert_heading.find_next_sibling('h3') if concert_heading else None
    if not details:
        return []

    lines = [clean_text(line) for line in details.get_text('\n').splitlines()]
    lines = [line for line in lines if line]
    description = '\n'.join(lines) or None
    records = []
    date_pattern = re.compile(
        r'^(\d{1,2}\.\d{1,2}\.\d{4}),\s*(.+?),\s*(\d{1,2}):(\d{2})h?\.?$'
    )
    for index, line in enumerate(lines):
        match = date_pattern.match(line)
        if not match or index == 0:
            continue
        event_date = parse_date(match.group(1))
        venue = clean_text(match.group(2))
        title = clean_text(lines[index - 1]).rstrip('.')
        city_match = re.search(r'\b(Solingen|Remscheid)\b', venue)
        if not event_date or not title or not venue or not city_match:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': SOURCE_URL,
            'time_from': f'{int(match.group(3)):02d}:{match.group(4)}',
            'venue': venue,
            'city': city_match.group(1),
            'country_code': 'DE',
            'description': description,
        })
    return records


def parse_schedule(html):
    soup = BeautifulSoup(html, 'html.parser')
    heading = next(
        (item for item in soup.select('.artikel h1') if item.get_text(strip=True).startswith('Spielzeit')),
        None,
    )
    container = heading.find_next_sibling(class_='copy') if heading else None
    if not container:
        return []

    records = []
    event_pattern = re.compile(
        r'(\d{1,2}\.\d{1,2}\.\d{2,4})\s*<br\s*/?>\s*'
        r'<strong[^>]*>(.*?)</strong>\s*<br\s*/?>\s*([^<]+?)\s*<br\s*/?>',
        re.I | re.S,
    )
    for match in event_pattern.finditer(str(container)):
        event_date = parse_date(match.group(1))
        title = clean_text(BeautifulSoup(match.group(2), 'html.parser').get_text(' ', strip=True))
        venue = clean_text(match.group(3))
        location = VENUE_LOCATIONS.get(venue)
        if not event_date or not title or not venue or not location:
            log_message(
                'Skipped incomplete Anthony Bramall concert',
                event='crawler_item_skipped',
                level='warning',
                url=SCHEDULE_URL,
                error_type='IncompleteEventData',
                error_message='Required date, title, venue, or defensible city is missing',
            )
            continue
        city, country_code = location
        records.append({
            'title': title,
            'date': event_date,
            'url': SCHEDULE_URL,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': title,
        })
    return records


class AnthonyBramallDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='anthony_bramall_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        for url, parser in ((SOURCE_URL, parse_homepage), (SCHEDULE_URL, parse_schedule)):
            try:
                response = requests.get(url, headers=HEADERS, timeout=45)
                response.raise_for_status()
                records.extend(parser(response.text))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Anthony Bramall calendar page',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    AnthonyBramallDeCrawler().run()


if __name__ == '__main__':
    main()
