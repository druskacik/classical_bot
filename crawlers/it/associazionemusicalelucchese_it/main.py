import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.associazionemusicalelucchese.it/'
CALENDAR_URL = urljoin(SOURCE_URL, 'stagioni/calendario/')
SOURCE = 'Associazione Musicale Lucchese'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\b', value)
    if not match:
        return None
    try:
        return date(
            int(match.group(3)), MONTHS[match.group(2).casefold()], int(match.group(1))
        ).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def parse_city(location_node):
    if location_node is None:
        return None
    lines = [clean_text(line) for line in location_node.stripped_strings]
    address = next((line for line in lines if ',' in line), '')
    parts = [part.strip() for part in address.split(',') if part.strip()]
    for index, part in enumerate(parts):
        if re.fullmatch(r'\d{5}', part) and index:
            city_index = index - 1
            if re.fullmatch(r'[A-Za-z]{2}', parts[city_index]) and city_index:
                city_index -= 1
            return parts[city_index]
    return None


def parse_detail(soup):
    event = soup.select_one('.em-event-single')
    if event is None:
        return None

    where = event.select_one('.em-event-where .em-event-location')
    venue_node = where.select_one('a[href*="/luogo/"]') if where else None
    venue = clean_text(venue_node)
    city = parse_city(where)
    description_node = event.select_one(':scope > section.em-event-content')
    if not venue or not city:
        return None
    return venue, city, clean_text(description_node) or None


def listing_items(soup):
    return soup.select('.em-events-list .em-item.em-event')


class AssociazioneMusicaleLuccheseItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='associazionemusicalelucchese_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        cards = []
        page_number = 1
        while True:
            try:
                soup = get_soup(session, CALENDAR_URL, {'pno': page_number})
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Associazione Musicale Lucchese calendar',
                    event='crawler_fetch_failed',
                    level='error',
                    url=CALENDAR_URL,
                    page_number=page_number,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            page_cards = listing_items(soup)
            if not page_cards:
                break
            cards.extend(page_cards)
            if not soup.select_one(f'a.page-numbers[href*="pno={page_number + 1}"]'):
                break
            page_number += 1

        records = []
        for card in cards:
            title_node = card.select_one('.em-item-title a')
            date_node = card.select_one('.em-event-date')
            time_node = card.select_one('.em-event-time')
            title = clean_text(title_node)
            event_date = parse_date(clean_text(date_node))
            url = urljoin(SOURCE_URL, title_node.get('href', '')) if title_node else ''
            if not title or not event_date or not url:
                continue

            try:
                detail = parse_detail(get_soup(session, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Associazione Musicale Lucchese event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if detail is None:
                log_message(
                    'Skipping event without a parseable venue and city',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                )
                continue

            venue, city, description = detail
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(clean_text(time_node)),
                'venue': venue,
                'city': city,
                'country_code': 'IT',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(
            records,
            key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['venue']),
        )


def main():
    AssociazioneMusicaleLuccheseItCrawler().run()


if __name__ == '__main__':
    main()
