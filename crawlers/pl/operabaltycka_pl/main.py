import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://operabaltycka.pl/'
REPERTOIRE_URL = urljoin(SOURCE_URL, 'repertuar')
FILTER_URL = urljoin(REPERTOIRE_URL + '/', 'filterAjax')
SOURCE = 'Opera Bałtycka'
HOME_VENUE = 'Opera Bałtycka'
HOME_CITY = 'Gdańsk'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def available_months(soup):
    return list(
        dict.fromkeys(
            option.get('value')
            for option in soup.select('#event-date-select option[value]')
            if re.fullmatch(r'\d{4}-\d{2}', option.get('value', ''))
        )
    )


def fetch_month_page(session, year_month, page):
    response = session.post(
        FILTER_URL,
        json={'page': str(page), 'filters': {'yearMonth': year_month}},
        headers={'Referer': REPERTOIRE_URL, 'Accept': 'application/json'},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return payload, BeautifulSoup(payload.get('entitiesHtml', ''), 'html.parser')


def listing_record(item):
    link = item.select_one('a[href*="/wydarzenie/"]')
    title_node = item.select_one('.repertoire-list-item__title')
    if not link or not title_node:
        return None

    url = urljoin(SOURCE_URL, link.get('href', ''))
    match = re.search(r'/(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2})(?:$|[/?#])', url)
    if not match:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    if not title:
        return None
    subtitle_node = item.select_one('.repertoire-list-item__desc')
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': match.group(2),
        '_listing_text': clean_text(item.get_text('\n', strip=True)),
        '_subtitle': clean_text(subtitle_node.get_text(' ', strip=True)) if subtitle_node else '',
    }


def detail_fields(session, record):
    soup = get_soup(session, record['url'])
    detail = soup.select_one('.event-detail')
    description = clean_text(detail.get_text('\n', strip=True)) if detail else None

    venue = ''
    for card in soup.select('.event-detail__card'):
        label = card.select_one('.event-detail__card-title')
        value = card.select_one('.event-detail__card-text')
        if label and value and clean_text(label.get_text(' ', strip=True)).casefold() == 'gdzie?':
            venue = clean_text(value.get_text(' ', strip=True))
            break

    evidence = f"{record['_listing_text']}\n{record['_subtitle']}"
    away_markers = ('poza siedzibą', 'wrocław', 'gdynia', 'poznań', 'sopot')
    appears_away = any(marker in evidence.casefold() for marker in away_markers)

    # Detail pages sometimes retain the home venue even when a particular
    # occurrence is explicitly advertised as touring. Such occurrences are
    # discarded unless the page supplies a usable away venue and city.
    if appears_away:
        known_away_venues = {
            'opera wrocławska': 'Wrocław',
            'teatr wielki w poznaniu': 'Poznań',
            'opera leśna': 'Sopot',
        }
        city = known_away_venues.get(venue.casefold())
        if not city:
            return None
    else:
        venue = venue or HOME_VENUE
        city = HOME_CITY

    return {
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listing_soup = get_soup(session, REPERTOIRE_URL)

    records_by_url = {}
    for year_month in available_months(listing_soup):
        page = 1
        while True:
            payload, soup = fetch_month_page(session, year_month, page)
            for item in soup.select('.repertoire-list-item'):
                record = listing_record(item)
                if record:
                    records_by_url[record['url']] = record
            pages = int(payload.get('pages') or 1)
            if page >= pages:
                break
            page += 1

    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(detail_fields, session, record): record
            for record in records_by_url.values()
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                detail = future.result()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=record['url'],
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if detail:
                record.update(detail)
                record.pop('_listing_text', None)
                record.pop('_subtitle', None)
                records.append(record)

    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['url']),
    )


class OperabaltyckaPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operabaltycka_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperabaltyckaPlCrawler().run()


if __name__ == '__main__':
    main()
