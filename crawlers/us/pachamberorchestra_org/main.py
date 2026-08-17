import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pachamberorchestra.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
VENUES_URL = urljoin(SOURCE_URL, 'venues')
SOURCE = 'Pennsylvania Chamber Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|'
        r'November|December)\s+(\d{1,2}),\s+(\d{4})\s+'
        r'(\d{1,2}):(\d{2})\s*([ap]m)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        parsed = datetime.strptime(' '.join(match.groups()), '%B %d %Y %I %M %p')
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def venue_cities(soup):
    cities = {}
    for venue in soup.select('.venues-wrapper[id]'):
        name = clean_text(venue.select_one('h3'))
        address = clean_text(venue.select_one('p.time'))
        match = re.search(r'([A-Za-z][A-Za-z .\'-]+),?\s+PA\s+\d{5}\b', address)
        if name and match:
            cities[venue['id']] = (name, match.group(1).strip(' ,'))
    return cities


def location_from_card(card, cities):
    link = card.select_one('a[href*="/venues/"]')
    venue = clean_text(link)
    if link is None or not venue:
        return None
    fragment = urlparse(urljoin(SOURCE_URL, link.get('href', ''))).fragment
    known = cities.get(fragment)
    if known and known[0].casefold() == venue.casefold():
        return known
    return None


def description_from_card(card):
    paragraphs = [
        clean_text(paragraph)
        for paragraph in card.select('.medium-4.cell > p:not(.time)')
    ]
    return '\n\n'.join(paragraph for paragraph in paragraphs if paragraph) or None


def parse_card(card, cities):
    title_link = card.select_one('h3 a[href]')
    title = clean_text(title_link)
    parsed_datetime = parse_datetime(clean_text(card.select_one('p.time')))
    location = location_from_card(card, cities)
    if title_link is None or not title or not parsed_datetime or not location:
        return None

    event_date, time_from = parsed_datetime
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(EVENTS_URL, title_link['href']),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description_from_card(card),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class PaChamberOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pachamberorchestra_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            events_response = session.get(EVENTS_URL, timeout=45)
            events_response.raise_for_status()
            venues_response = session.get(VENUES_URL, timeout=45)
            venues_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Pennsylvania Chamber Orchestra pages',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        events_soup = BeautifulSoup(events_response.text, 'html.parser')
        cities = venue_cities(BeautifulSoup(venues_response.text, 'html.parser'))
        records = []
        for card in events_soup.select('.concert-wrapper'):
            record = parse_card(card, cities)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped event with incomplete date or location',
                    event='crawler_item_skipped',
                    level='warning',
                    url=EVENTS_URL,
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    PaChamberOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
