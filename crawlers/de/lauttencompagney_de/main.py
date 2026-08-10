import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lauttencompagney.de/'
SOURCE = 'lautten compagney BERLIN'
CALENDAR_URL = f'{SOURCE_URL}kalender/'
HEADERS = {'User-Agent': 'classical-concert-crawler/1.0'}
MONTHS = {
    'jan': 1, 'feb': 2, 'mär': 3, 'mar': 3, 'apr': 4, 'mai': 5,
    'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'okt': 10,
    'nov': 11, 'dez': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(date_box):
    spans = date_box.select('span') if date_box else []
    if len(spans) < 3:
        return None
    day_match = re.search(r'\d{1,2}', spans[1].get_text())
    month_year = spans[2].get_text(' ', strip=True).replace('\xa0', ' ')
    match = re.search(r'([A-Za-zÄÖÜäöü]{3})\s+(20\d{2})', month_year)
    if not day_match or not match:
        return None
    month = MONTHS.get(match.group(1).casefold())
    if not month:
        return None
    try:
        return date(int(match.group(2)), month, int(day_match.group())).isoformat()
    except ValueError:
        return None


def parse_time(date_box):
    if date_box is None:
        return None
    match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)', date_box.get_text(' ', strip=True))
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def description_for(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('main .content') or soup.select_one('main')) or None


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('#termin-liste .termin'):
        event_link = item.select_one('a[href*="/programm/"]')
        details = item.select_one('.details')
        title_node = details.select_one('h1') if details else None
        city_node = details.select_one('h4.red') if details else None
        event_date = parse_date(item.select_one('.datum'))
        title = clean_text(title_node)
        city = clean_text(city_node)
        url = event_link.get('href', '').strip() if event_link else ''
        if not all((title, event_date, city, url, details)):
            log_message(
                'Skipped incomplete lautten compagney calendar item',
                event='crawler_item_skipped', level='warning', url=url or CALENDAR_URL,
                error_type='IncompleteEventData',
                error_message='Missing title, date, city, or programme URL',
            )
            continue

        details_copy = BeautifulSoup(str(details), 'html.parser')
        for heading in details_copy.select('h1, h2, h3, h4'):
            heading.decompose()
        venue = clean_text(details_copy)
        # The lc :am leo series uses its fixed Berlin church inconsistently:
        # some entries put the church in the city slot, while others put
        # Berlin there and use the remaining text for the individual title.
        if title.casefold() == 'lc :am leo':
            if venue:
                title = re.sub(
                    r'^:kiez kirchen konzerte\s*[-–]\s*', '', venue,
                    flags=re.I,
                ) or title
            city, venue = 'Berlin', 'Alte Nazarethkirche'
        if not venue:
            log_message(
                'Skipped lautten compagney item without venue',
                event='crawler_item_skipped', level='warning', url=url,
                error_type='IncompleteEventData', error_message=f'Missing venue for {event_date}',
            )
            continue
        records.append({
            'title': title, 'date': event_date, 'url': url,
            'time_from': parse_time(item.select_one('.datum')),
            'venue': venue, 'city': city,
            'country_code': 'CH' if city == 'Zürich' else 'DE',
            'description': None, 'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


class LauttencompagneyDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lauttencompagney_de', source=SOURCE, source_url=SOURCE_URL,
        country_code='DE', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        for params in ({}, {'archive': 'true'}):
            response = requests.get(
                CALENDAR_URL, params=params, headers=HEADERS, timeout=45,
            )
            response.raise_for_status()
            records.extend(parse_calendar(response.text))

        descriptions = {}
        urls = sorted({record['url'] for record in records})
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(description_for, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    descriptions[url] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Could not fetch lautten compagney programme description',
                        event='crawler_detail_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
        for record in records:
            record['description'] = descriptions.get(record['url'])
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    LauttencompagneyDeCrawler().run()


if __name__ == '__main__':
    main()
