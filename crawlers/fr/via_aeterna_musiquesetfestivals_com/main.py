import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://via-aeterna.musiquesetfestivals.com/'
SOURCE = 'Via Aeterna'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/show'

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
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def valid_date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def get_response(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def show_urls(session):
    """Return every currently published show, across all API pages."""
    urls = []
    page = 1
    while True:
        response = get_response(
            session,
            API_URL,
            {'page': page, 'per_page': 100, '_fields': 'link'},
        )
        items = response.json()
        urls.extend(item.get('link') for item in items if item.get('link'))
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(urls))


def description_from_page(soup):
    content = soup.select_one('.show-single__content_main')
    if not content:
        return None

    clone = BeautifulSoup(str(content), 'html.parser')
    # Keep presentation, programme, and distribution, but not ticket prices,
    # travel directions, or unrelated event cards.
    for selector in (
        '.show-single__prices',
        '.show-single__ticketing',
        '.show-single__related',
        '.stage-card',
        'script',
        'style',
    ):
        for element in clone.select(selector):
            element.decompose()
    return clean_text(clone) or None


def parse_show(session, url):
    soup = BeautifulSoup(get_response(session, url).content, 'html.parser')
    title = clean_text(soup.select_one('h1'))
    date_element = soup.select_one('time.show-single__date[datetime]')
    event_date = valid_date(date_element.get('datetime') if date_element else None)
    time_element = soup.select_one('time.show-single__time[datetime]')
    datetime_value = time_element.get('datetime', '') if time_element else ''
    time_match = re.search(r'T(\d{2}:\d{2})', datetime_value)

    city = clean_text(soup.select_one('.show-single__place'))
    venue = clean_text(soup.select_one('.stage-card__name > a'))
    if not venue:
        venue = clean_text(soup.select_one('.stage-card__name'))

    if not title or not event_date or not city or not venue:
        log_message(
            'Skipping Via Aeterna item with incomplete required fields',
            event='crawler_item_skipped',
            level='warning',
            url=url,
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(1) if time_match else None,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description_from_page(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ViaAeternaMusiquesEtFestivalsComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='via_aeterna_musiquesetfestivals_com',
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
        urls = show_urls(session)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(parse_show, session, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                    if record:
                        records.append(record)
                except (requests.RequestException, ValueError, TypeError) as error:
                    log_message(
                        'Failed to scrape Via Aeterna item',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    return ViaAeternaMusiquesEtFestivalsComCrawler().run()


if __name__ == '__main__':
    main()
