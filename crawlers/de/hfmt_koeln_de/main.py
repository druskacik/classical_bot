import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.hfmt-koeln.de/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Hochschule für Musik und Tanz Köln'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u00ad', '').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(url, parser='html.parser'):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def discover_event_urls():
    index = get_soup(SITEMAP_URL, 'xml')
    sitemap_urls = [
        clean_text(node)
        for node in index.find_all('loc')
        if 'sitemap=events' in clean_text(node)
    ]
    if not sitemap_urls:
        raise ValueError('No event sitemaps found in sitemap index')

    event_urls = []
    for sitemap_url in sitemap_urls:
        sitemap = get_soup(sitemap_url, 'xml')
        event_urls.extend(clean_text(node) for node in sitemap.find_all('loc'))
    return list(dict.fromkeys(url for url in event_urls if url))


def table_value(soup, label):
    for row in soup.select('.c-event-details__table tr'):
        heading = row.find('th')
        if clean_text(heading).casefold() == label.casefold():
            return row.find('td')
    return None


def parse_datetime(soup):
    value = clean_text(table_value(soup, 'Datum'))
    date_match = re.search(r'\b(\d{2})\.(\d{2})\.(\d{4})\b', value)
    if not date_match:
        return None, None
    try:
        event_date = date(
            int(date_match.group(3)), int(date_match.group(2)), int(date_match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]\d|2[0-3]):([0-5]\d)\b', value)
    event_time = f'{time_match.group(1)}:{time_match.group(2)}' if time_match else None
    return event_date, event_time


def parse_location(soup):
    value = table_value(soup, 'Ort')
    if not value:
        return None, None

    lines = [clean_text(part) for part in value.stripped_strings]
    lines = [line for line in lines if line]
    if not lines:
        return None, None
    location_text = ' '.join(lines)

    postal_match = re.search(r'\b\d{5}\s+([A-ZÄÖÜ][\wÄÖÜäöüß.-]*(?:[ -][A-ZÄÖÜ][\wÄÖÜäöüß.-]*)*)', location_text)
    city = postal_match.group(1).strip() if postal_match else None
    if not city:
        for known_city in ('Köln', 'Wuppertal', 'Aachen'):
            if re.search(rf'\b{re.escape(known_city)}\b', location_text, re.IGNORECASE):
                city = known_city
                break

    venue = lines[0]
    if city and venue.casefold() == city.casefold():
        venue = ''
    return (venue or None), city


def parse_event(url, soup):
    title = clean_text(soup.select_one('.c-event-intro__headline'))
    event_date, event_time = parse_datetime(soup)
    venue, city = parse_location(soup)
    description = clean_text(
        soup.select_one('.c-event-details > .o-column-container__column--leading')
    ) or None
    if not title or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_event(url):
    return parse_event(url, get_soup(url))


class HfmtKoelnDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hfmt_koeln_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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

    def __init__(self, event_urls=None):
        self.event_urls = event_urls

    def scrape(self):
        urls = self.event_urls if self.event_urls is not None else discover_event_urls()
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(scrape_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    HfmtKoelnDeCrawler().run()


if __name__ == '__main__':
    main()
