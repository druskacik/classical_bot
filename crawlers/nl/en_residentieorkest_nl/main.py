import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://en.residentieorkest.nl/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda?type=concerts')
SOURCE = 'Residentie Orkest'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,nl;q=0.7',
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
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def split_location(value):
    parts = [part.strip() for part in clean_text(value).split(',') if part.strip()]
    if len(parts) == 1:
        venue = parts[0]
        if 'amare' in venue.lower():
            return venue, 'The Hague'
        if venue.lower() == 'heerlen theater':
            return venue, 'Heerlen'
        return None, None
    if not parts:
        return None, None
    return ', '.join(parts[:-1]), parts[-1]


def country_for_city(city):
    return 'DE' if city == 'Ludwigshafen' else 'NL'


def occurrence_url(url, date_value, time_value):
    parsed = urlparse(urljoin(SOURCE_URL, url))
    query = parse_qs(parsed.query)
    query['concertDate'] = [f'{date_value}_{time_value}']
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def agenda_occurrences(session):
    soup = get_soup(session, AGENDA_URL)
    occurrences = []
    for card in soup.select('a.concert__link[href*="/concert/"]'):
        parsed = urlparse(urljoin(SOURCE_URL, card.get('href')))
        selected = parse_qs(parsed.query).get('concertDate', [''])[0]
        match = re.fullmatch(r'(\d{4})-(\d{2})-(\d{2})_(\d{2}:\d{2})', selected)
        if not match:
            continue
        year, month, _, time_value = match.groups()
        venue, city = split_location(card.select_one('.concert__location div:nth-of-type(2)'))
        title = clean_text(card.select_one('.concert__title'))
        if not title or not venue or not city:
            continue

        date_nodes = card.select('.concert__dateblock .d-flex.text-center > div')
        dates = []
        for node in date_nodes:
            day = clean_text(node.select_one('.day'))
            month_text = clean_text(node.select_one('.month'))
            try:
                parsed_date = datetime.strptime(f'{day} {month_text} {year}', '%d %b %Y')
            except ValueError:
                continue
            dates.append(parsed_date.date().isoformat())
        if not dates:
            dates = [f'{year}-{month}-{match.group(3)}']

        for date_value in dates:
            occurrences.append({
                'title': title,
                'date': date_value,
                'url': occurrence_url(card.get('href'), date_value, time_value),
                'time_from': time_value,
                'venue': venue,
                'city': city,
            })
    return occurrences


def detail_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for node in soup.select('main .block__content, main .dropdown'):
        text = clean_text(node)
        if not text or text in parts:
            continue
        # Ticket panels add no programme evidence and can be very volatile.
        if text.lower().startswith(('tickets', 'share')):
            continue
        parts.append(text)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    occurrences = agenda_occurrences(session)
    descriptions = {}
    detail_urls = sorted({record['url'] for record in occurrences})
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                descriptions[url] = None
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    for occurrence in occurrences:
        records.append({
            **occurrence,
            'country_code': country_for_city(occurrence['city']),
            'description': descriptions.get(occurrence['url']),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ResidentieOrkestEnCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='en_residentieorkest_nl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ResidentieOrkestEnCrawler().run()


if __name__ == '__main__':
    main()
