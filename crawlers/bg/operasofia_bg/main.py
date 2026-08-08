import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operasofia.bg/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'Софийска опера и балет'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'bg-BG,bg;q=0.9,en;q=0.7',
}

# The calendar sometimes contains performances away from the opera building.
# Only infer a city when the published address contains an unambiguous place name.
BG_CITIES = (
    'София',
    'Банкя',
    'Банско',
    'Белоградчик',
    'Благоевград',
    'Бургас',
    'Варна',
    'Велико Търново',
    'Видин',
    'Враца',
    'Добрич',
    'Казанлък',
    'Кюстендил',
    'Плевен',
    'Пловдив',
    'Правец',
    'Русе',
    'Стара Загора',
    'Шумен',
)

BG_MONTHS = {
    'януари': 1,
    'февруари': 2,
    'март': 3,
    'април': 4,
    'май': 5,
    'юни': 6,
    'юли': 7,
    'август': 8,
    'септември': 9,
    'октомври': 10,
    'ноември': 11,
    'декември': 12,
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


def calendar_pages(session):
    soup = get_soup(session, CALENDAR_URL)
    urls = {
        urljoin(SOURCE_URL, link.get('href'))
        for link in soup.select('a.select[href*="/calendar/"]')
        if re.search(r'/calendar/\d{4}-\d{2}(?:\?|$)', link.get('href') or '')
    }
    # Include the redirected current month even if a temporary template change
    # removes it from the season selector.
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and '/calendar/' in (canonical.get('href') or ''):
        urls.add(urljoin(SOURCE_URL, canonical.get('href')))
    return sorted(urls)


def resolve_city(address):
    folded = address.casefold()
    for city in BG_CITIES:
        if re.search(rf'(?<![\w]){re.escape(city.casefold())}(?![\w])', folded):
            return city
    return None


def parse_month(soup, month_url):
    month_match = re.search(r'/calendar/(\d{4})-(\d{2})', month_url)
    if not month_match:
        return []
    year, month = map(int, month_match.groups())
    displayed = clean_text(soup.select_one('#calendarDates')).casefold()
    displayed_match = re.search(r'([а-я]+)\s+(\d{4})', displayed)
    if not displayed_match:
        return []
    displayed_month = BG_MONTHS.get(displayed_match.group(1))
    if (int(displayed_match.group(2)), displayed_month) != (year, month):
        # Invalid/empty archive routes fall back to the current month while
        # keeping the requested URL. Never assign that content fabricated dates.
        return []
    records = []
    for section in soup.select('.page-calendar__content-wrapper .section'):
        day_node = section.select_one('.calendar-date .day-number')
        if not day_node:
            continue
        try:
            event_date = date(year, month, int(clean_text(day_node))).isoformat()
        except (TypeError, ValueError):
            continue

        for item in section.select('article.item'):
            link = item.select_one('.item__description a[href*="/repertoire/"]')
            title = clean_text(item.select_one('.item__description__name'))
            time_from = clean_text(item.select_one('.start-time__hour'))
            venue = clean_text(item.select_one('.item__location__scene'))
            address = clean_text(item.select_one('.item__location__address'))
            city = resolve_city(address)
            url = urljoin(SOURCE_URL, link.get('href')) if link else ''
            summary = clean_text(item.select_one('.item__description__author'))
            if not all((title, url, venue, city)):
                continue
            if not re.fullmatch(r'\d{2}:\d{2}', time_from):
                time_from = None
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'BG',
                'description': summary or None,
            })
    return records


def repertoire_description(session, url):
    soup = get_soup(session, url)
    parts = []
    for selector in (
        '.page-repertoire__performance__heading .item__description__author',
        '.page-repertoire__performance__review',
        '.page-repertoire__performance__synopsis',
        '.page-repertoire__performance__staff .player-cards:first-of-type',
    ):
        value = clean_text(soup.select_one(selector))
        if value and value not in parts:
            parts.append(value)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    month_urls = calendar_pages(session)
    records = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in month_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_month(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    descriptions = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(repertoire_description, session, url): url
            for url in {record['url'] for record in records}
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape repertoire detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    for record in records:
        detail = descriptions.get(record['url'])
        summary = record['description']
        if detail and summary and summary not in detail:
            record['description'] = f'{summary}\n\n{detail}'
        elif detail:
            record['description'] = detail

    unique_records = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique_records.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class OperasofiaBgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operasofia_bg',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BG',
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
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperasofiaBgCrawler().run()


if __name__ == '__main__':
    main()
