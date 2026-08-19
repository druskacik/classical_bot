import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.sarasotaopera.org/'
CONCERTS_URL = urljoin(SOURCE_URL, 'concerts')
SOURCE = 'Sarasota Opera'
VENUE = 'Sarasota Opera House'
CITY = 'Sarasota'
COUNTRY_CODE = 'US'


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


def normalized_title(value):
    return re.sub(r'[^a-z0-9]+', '', clean_text(value).lower())


def production_date_range(value):
    match = re.fullmatch(
        r'([A-Z][a-z]+) (\d{1,2})(?:\s*&\s*(\d{1,2})|\s*-\s*([A-Z][a-z]+) (\d{1,2})), (\d{4})',
        clean_text(value),
    )
    if not match:
        return None, None
    first_month, first_day, second_same_month_day, second_month, second_day, year = match.groups()
    start = datetime.strptime(f'{first_month} {first_day}, {year}', '%B %d, %Y').date()
    end_month = second_month or first_month
    end_day = second_day or second_same_month_day
    end = datetime.strptime(f'{end_month} {end_day}, {year}', '%B %d, %Y').date()
    return start, end


def current_season_url(soup):
    for link in soup.select('a[href]'):
        if clean_text(link.get_text(' ', strip=True)).lower() == 'operas':
            return urljoin(SOURCE_URL, link.get('href'))
    raise ValueError('Could not locate the current opera season page')


def season_productions(soup):
    productions = {}
    for card in soup.select('main a.card-container-link[href*="/event/"]'):
        title_node = card.select_one('.cards-item-header')
        if not title_node:
            continue
        title = clean_text(title_node.get_text(' ', strip=True))
        url = urljoin(SOURCE_URL, card.get('href'))
        if title and url:
            date_node = card.select_one('.cards-item-description')
            start_date, end_date = production_date_range(
                date_node.get_text(' ', strip=True) if date_node else ''
            )
            productions[normalized_title(title)] = {
                'title': title,
                'url': url,
                'start_date': start_date,
                'end_date': end_date,
            }
    return productions


def parse_homepage_occurrences(soup, productions):
    records = []
    for item in soup.select('.my-slider-events > [data-event-date]'):
        title_node = item.select_one('.slide-content-header')
        time_node = item.select_one('.slide-content-time')
        ticket_link = item.select_one('a[href*="tickets.sarasotaopera.org"]')
        if not title_node or not ticket_link:
            continue
        listing_title = normalized_title(title_node.get_text(' ', strip=True))
        production = next(
            (value for key, value in productions.items() if listing_title == key or listing_title.startswith(key)),
            None,
        )
        if not production:
            continue
        try:
            event_date = datetime.strptime(item['data-event-date'], '%Y_%m_%d').date().isoformat()
        except (KeyError, ValueError):
            continue
        if production['start_date'] and not (
            production['start_date'].isoformat() <= event_date <= production['end_date'].isoformat()
        ):
            continue
        time_from = None
        if time_node:
            try:
                time_from = datetime.strptime(
                    clean_text(time_node.get_text(' ', strip=True)), '%I:%M %p'
                ).strftime('%H:%M')
            except ValueError:
                pass
        records.append(make_record(
            title=production['title'],
            event_date=event_date,
            url=urljoin(SOURCE_URL, ticket_link.get('href')),
            time_from=time_from,
            description=None,
            detail_url=production['url'],
        ))
    return records


def parse_concerts(soup):
    records = []
    for card in soup.select('main a.card-container-link[href*="tickets.sarasotaopera.org"]'):
        title_node = card.select_one('.cards-item-header')
        date_node = card.select_one('.cards-item-pre-header')
        description_node = card.select_one('.cards-item-description')
        if not title_node or not date_node:
            continue
        date_text = clean_text(date_node.get_text(' ', strip=True))
        match = re.match(r'([A-Z][a-z]+ \d{1,2}, \d{4})(.*)', date_text)
        if not match:
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        time_from = None
        time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)', match.group(2), re.I)
        if not time_match:
            time_match = re.search(
                r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)',
                clean_text(title_node.get_text(' ', strip=True)),
                re.I,
            )
        if not time_match and re.search(
            r'\bat noon\b',
            f'{date_text} {clean_text(title_node.get_text(" ", strip=True))}',
            re.I,
        ):
            time_from = '12:00'
        if time_match:
            raw_time = f'{time_match.group(1)}:{time_match.group(2) or "00"} {time_match.group(3)}'
            time_from = datetime.strptime(raw_time, '%I:%M %p').strftime('%H:%M')
        records.append(make_record(
            title=clean_text(title_node.get_text(' ', strip=True)),
            event_date=event_date,
            url=urljoin(SOURCE_URL, card.get('href')),
            time_from=time_from,
            description=(
                clean_text(description_node.get_text(' ', strip=True))
                if description_node else None
            ),
        ))
    return records


def make_record(title, event_date, url, time_from, description, detail_url=None):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': VENUE,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
        '_detail_url': detail_url,
    }


def detail_description(session, url):
    soup = get_soup(session, url)
    main = soup.select_one('main')
    return clean_text(main.get_text('\n', strip=True)) if main else None


def get_concerts():
    session = requests.Session(impersonate='chrome')
    home_soup = get_soup(session, SOURCE_URL)
    season_soup = get_soup(session, current_season_url(home_soup))
    concerts_soup = get_soup(session, CONCERTS_URL)
    records = parse_homepage_occurrences(home_soup, season_productions(season_soup))
    records.extend(parse_concerts(concerts_soup))

    detail_urls = {record['_detail_url'] for record in records if record['_detail_url']}
    descriptions = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(detail_description, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestsError as error:
                log_message(
                    'Failed to scrape opera detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    unique = {}
    for record in records:
        detail_url = record.pop('_detail_url')
        if detail_url and descriptions.get(detail_url):
            record['description'] = descriptions[detail_url]
        key = (record['url'], record['date'], record['time_from'])
        unique[key] = record
    return sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class SarasotaoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sarasotaopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SarasotaoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
