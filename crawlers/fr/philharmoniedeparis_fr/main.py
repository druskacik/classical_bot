from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://philharmoniedeparis.fr/fr'
SOURCE = 'Philharmonie de Paris'
AGENDA_API_URL = f'{SOURCE_URL}/agenda-ajax'
ARCHIVE_START_DATE = '1900-01-01'
PARIS_TIMEZONE = ZoneInfo('Europe/Paris')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    return ' '.join(text.replace('\xa0', ' ').replace('\u202f', ' ').split())


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))


def parse_card(card):
    link = card.select_one('a[href*="/activite/"]')
    title = clean_text(card.select_one('.EventCard-title'))
    venue = clean_text(card.select_one('.EventCard-place'))
    timestamp = card.get('data-timestamp')
    try:
        occurrence = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(PARIS_TIMEZONE)
    except (TypeError, ValueError, OverflowError):
        return None

    url = canonical_url(link.get('href') if link else '')
    if not title or not venue or not url:
        return None

    description_parts = []
    for selector in ('.EventCard-category', '.EventCard-subtitle', '.EventCard-description'):
        text = clean_text(card.select_one(selector))
        if text and text not in description_parts:
            description_parts.append(text)

    return {
        'title': title,
        'date': occurrence.date().isoformat(),
        'url': url,
        'time_from': occurrence.strftime('%H:%M'),
        'venue': venue,
        'city': 'Paris',
        'country_code': 'FR',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PhilharmonieDeParisFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='philharmoniedeparis_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        params = {
            'types': '1',
            'op': 'init',
            'startDate': ARCHIVE_START_DATE,
        }
        records = []
        seen = set()
        page = 1

        while True:
            response = session.get(AGENDA_API_URL, params=params, timeout=60)
            response.raise_for_status()
            payload = response.json()
            content = payload.get('content') or ''
            cards = BeautifulSoup(content, 'html.parser').select('article.EventCard')

            for card in cards:
                record = parse_card(card)
                if record:
                    key = (record['url'], record['date'], record['time_from'], record['venue'])
                    if key not in seen:
                        seen.add(key)
                        records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Philharmonie de Paris performance',
                        event='crawler_item_skipped',
                        level='warning',
                        url=SOURCE_URL,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )

            if not payload.get('moreEvents'):
                break
            if not cards:
                raise RuntimeError('Agenda API advertised more events but returned no event cards')

            page += 1
            params = {
                'types': '1',
                'op': 'more',
                'page': str(page),
                'last_date': payload.get('lastDate', ''),
                'last_group': str(payload.get('lastGroup', '')),
                'weekends': payload.get('weekends', ''),
                'periods': payload.get('periods', ''),
                'onlyActivities': str(payload.get('onlyActivities', False)).lower(),
            }

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    PhilharmonieDeParisFrCrawler().run()


if __name__ == '__main__':
    main()
