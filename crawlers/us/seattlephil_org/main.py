import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://seattlephil.org/'
LISTING_URL = f'{SOURCE_URL}concerts-and-tickets/'
SOURCE = 'Seattle Philharmonic Orchestra'
CITY = 'Seattle'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value):
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)\b', '', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = re.sub(r'\s+', ' ', clean_text(value)).upper()
    value = re.sub(r'(?<=\d)(AM|PM)$', r' \1', value)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def detail_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    heading = soup.select_one('main h1')
    container = heading.find_parent('div', class_='col-sm-8') if heading else None
    if not container:
        return None

    paragraphs = []
    for node in container.select('p'):
        text = clean_text(node)
        if text and text not in paragraphs:
            paragraphs.append(text)
    return '\n\n'.join(paragraphs) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    records = []
    for card in soup.select('.concert-card'):
        title = clean_text(card.select_one('.concert-card__right h3'))
        event_date = parse_date(card.select_one('.concert-card__left__content__date'))
        time_from = parse_time(card.select_one('.concert-card__left__content__time'))
        venue = clean_text(card.select_one('.concert-card__left__content__location'))
        link = card.select_one('.concert-card__left__content a[href]')
        url = link.get('href', '').strip() if link else ''

        if not title or not event_date or not venue or not url.startswith(('http://', 'https://')):
            continue

        description = detail_description(session, url)
        if not description:
            description = clean_text(card.select_one('.concert-card__right p')) or None

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No concert cards found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SeattlePhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='seattlephil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    SeattlePhilOrgCrawler().run()


if __name__ == '__main__':
    main()
