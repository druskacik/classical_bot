import re
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://capetownopera.co.za/'
EVENTS_URL = urljoin(SOURCE_URL, 'cape-town-opera-events/')
SOURCE = 'Cape Town Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-ZA,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def ticket_urls(session):
    """Discover ticket detail pages selected by Cape Town Opera itself."""
    urls = {}
    for listing_url in (SOURCE_URL, EVENTS_URL):
        soup = get_soup(session, listing_url)
        for link in soup.select('a[href*="webtickets.co.za"][href*="itemid="]'):
            url = urljoin(listing_url, link.get('href', ''))
            item_id = parse_qs(urlparse(url).query).get('itemid', [''])[0]
            if item_id.isdigit():
                urls[item_id] = url
    return list(urls.values())


def venue_and_city(raw_venue):
    value = clean_text(raw_venue)
    lower = value.lower()
    if 'cape town' in lower:
        city = 'Cape Town'
    elif 'johannesburg' in lower or 'fourways' in lower or 'montecasino' in lower:
        city = 'Johannesburg'
    else:
        return None

    # Webtickets appends room, street and city as comma-separated address
    # components. Keep only the named venue and, for Artscape, its room.
    parts = [part.strip() for part in value.split(',') if part.strip()]
    if not parts:
        return None
    if parts[0].lower() == 'artscape' and len(parts) > 1:
        venue = f'{parts[0]} {parts[1]}'
    else:
        venue = parts[0]
    return venue, city


def parse_ticket_page(soup, url):
    title_node = soup.select_one('h2.h2')
    venue_node = soup.select_one('#EventPanel_tbxVenue')
    description_node = soup.select_one('#EventPanel_event_description')
    if not title_node or not venue_node:
        return []

    title = clean_text(title_node.get_text(' ', strip=True))
    location = venue_and_city(venue_node.get_text(' ', strip=True))
    if not title or not location:
        return []
    venue, city = location
    description = clean_text(description_node) or None

    records = []
    for node in soup.select('h4.h5.text-body.mb-0'):
        value = clean_text(node.get_text(' ', strip=True))
        try:
            start = datetime.strptime(value, '%d-%b-%Y %H:%M')
        except ValueError:
            continue
        records.append(
            {
                'title': title,
                'date': start.date().isoformat(),
                'url': url,
                'time_from': start.strftime('%H:%M'),
                'venue': venue,
                'city': city,
                'country_code': 'ZA',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in ticket_urls(session):
        try:
            records.extend(parse_ticket_page(get_soup(session, url), url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape ticket detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'], item['title'], item['url']),
    )


class CapetownoperaCoZaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='capetownopera_co_za',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ZA',
        upload_target='potential',
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
        return get_concerts()


def main():
    CapetownoperaCoZaCrawler().run()


if __name__ == '__main__':
    main()
