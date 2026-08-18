from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://conservatoire.grandpoitiers.fr/'
AGENDA_URL = urljoin(SOURCE_URL, 'informations-transversales/agenda')
SOURCE = 'Conservatoire de Grand Poitiers'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    return '\n'.join(line.strip() for line in text.replace('\xa0', ' ').splitlines() if line.strip())


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def parse_listing(html):
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for card in soup.select('article.events-block__item'):
        link = card.select_one('.events-block__title a[href]')
        date_node = card.select_one('time.date__time[datetime]')
        venue_node = card.select_one('.time-place__item.-place span')
        times = [node.get('datetime') for node in card.select('.time-place__item.-time time[datetime]')]
        starts = times[::2] or [None]
        items.append({
            'title': clean_text(link),
            'date': valid_date(date_node.get('datetime') if date_node else None),
            'url': canonical_url(link.get('href') if link else ''),
            'listing_venue': clean_text(venue_node),
            'starts': starts,
        })
    next_link = soup.select_one('nav[aria-label="Pagination"] a[rel="next"]')
    if not next_link:
        next_link = next(
            (link for link in soup.select('nav[aria-label="Pagination"] a[href]')
             if 'Suivant' in clean_text(link)),
            None,
        )
    return items, urljoin(AGENDA_URL, next_link.get('href')) if next_link else None


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    location = clean_text(soup.select_one('.heading .time-place__item.-place'))
    location_parts = [part.strip() for part in location.split('\n') if part.strip() != '-']
    if len(location_parts) >= 2:
        city, venue = location_parts[0].rstrip(' -'), ' '.join(location_parts[1:])
    else:
        city, venue = '', location
    description = clean_text(soup.select_one('.site-content .rte')) or None
    return city.strip(), venue.strip(), description


class ConservatoireGrandPoitiersFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='conservatoire_grandpoitiers_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        items = []
        page_url = AGENDA_URL
        seen_pages = set()
        while page_url and page_url not in seen_pages:
            seen_pages.add(page_url)
            response = session.get(page_url, timeout=45)
            response.raise_for_status()
            page_items, page_url = parse_listing(response.text)
            items.extend(page_items)

        details = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(session.get, url, timeout=45): url
                for url in sorted({item['url'] for item in items if item['url']})
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    details[url] = parse_detail(response.text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Conservatoire event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for item in items:
            city, venue, description = details.get(item['url'], ('', item['listing_venue'], None))
            venue = venue or item['listing_venue']
            if not item['title'] or not item['date'] or not item['url'] or not city or not venue:
                log_message(
                    'Skipped incomplete Conservatoire event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=item['url'] or AGENDA_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, city, or venue is missing',
                )
                continue
            for start in item['starts']:
                records.append({
                    'title': item['title'],
                    'date': item['date'],
                    'url': item['url'],
                    'time_from': start,
                    'venue': venue,
                    'city': city,
                    'description': description,
                })
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ConservatoireGrandPoitiersFrCrawler().run()


if __name__ == '__main__':
    main()
