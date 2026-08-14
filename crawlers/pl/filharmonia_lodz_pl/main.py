import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.filharmonia.lodz.pl/'
SOURCE = 'Filharmonia Łódzka im. Artura Rubinsteina'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
EVENT_PATH = '/repertuar-filharmonii-lodzkiej/'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalConcertCrawler/1.0)',
    'Accept-Language': 'pl-PL,pl;q=0.9',
}
MONTHS = {
    'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4,
    'maja': 5, 'czerwca': 6, 'lipca': 7, 'sierpnia': 8,
    'września': 9, 'wrzesnia': 9, 'października': 10,
    'pazdziernika': 10, 'listopada': 11, 'grudnia': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_datetime(value):
    normalized = value.casefold()
    match = re.search(
        r'\b(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(20\d{2})\b', normalized
    )
    if not match or match.group(2) not in MONTHS:
        return None, None
    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))
        ).isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\b([01]?\d|2[0-3])[:.]([0-5]\d)\b', value)
    event_time = (
        f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
        if time_match else None
    )
    return event_date, event_time


def parse_location(value, title):
    value = re.sub(r'\s*\([^)]*\)\s*$', '', value).strip(' ,')
    if not value:
        return '', ''

    # Locations are normally "venue, city" (and occasionally include a street
    # address between them). The institution's halls omit the home city.
    parts = [part.strip() for part in value.split(',') if part.strip()]
    city = ''
    if len(parts) > 1 and not re.match(r'^(?:ul\.|al\.|pl\.)\s', parts[-1], re.I):
        city = parts[-1]
    title_city = re.search(r'\b(?:w|we)\s+([A-ZŁŚŻŹĆŃÓ][\wąćęłńóśźż-]+)', title)
    if not city and title_city:
        city = title_city.group(1)
    if not city:
        city = 'Łódź'

    venue = parts[0]
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if main is None:
        return None
    title = clean_text(main.select_one('.main-title-margin')) or clean_text(
        main.select_one('.banner__title')
    )
    metrics = [clean_text(item) for item in main.select('.metric__item')]
    date_value = next((value for value in metrics if re.search(r'\b20\d{2}\b', value)), '')
    event_date, event_time = parse_datetime(date_value)
    location = next(
        (
            value for value in reversed(metrics[2:])
            if value != date_value
            and not re.search(r'\b(?:ceny?|bilet|wstęp wolny)\b', value, re.I)
            and len(value) < 180
            and not re.search(r'\b(?:dyrygent|solista|orkiestra|kompozytor|nazwa utworu)\b', value, re.I)
            and re.search(
                r'\b(?:sala|filharmoni|kościół|bazylika|katedra|plac|pałac|zamek|'
                r'amfiteatr|muzeum|park|centrum|dom kultury|teatr|studio|klub|'
                r'klasztor|synagoga|arena|akademia|szkoła|dwór)\b|,',
                value,
                re.I,
            )
        ),
        'Sala koncertowa Filharmonii Łódzkiej',
    )
    venue, city = parse_location(location, title)
    if not all((title, event_date, venue, city)):
        return None

    description_parts = []
    seen = set()
    for block in main.select('.ckeditor'):
        text = clean_text(block)
        if text and text not in seen:
            description_parts.append(text)
            seen.add(text)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FilharmoniaLodzPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_lodz_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def _event_urls(self, session):
        response = session.get(SITEMAP_URL, timeout=45)
        response.raise_for_status()
        index = BeautifulSoup(response.content, 'xml')
        sitemap_urls = [loc.get_text(strip=True) for loc in index.find_all('loc')]
        event_urls = set()
        for sitemap_url in sitemap_urls:
            page = session.get(sitemap_url, timeout=45)
            page.raise_for_status()
            sitemap = BeautifulSoup(page.content, 'xml')
            event_urls.update(
                loc.get_text(strip=True) for loc in sitemap.find_all('loc')
                if EVENT_PATH in loc.get_text(strip=True)
            )
        return sorted(event_urls)

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        urls = self._event_urls(session)
        records = []

        def fetch(url):
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return parse_event(response.text, url)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Filharmonia Łódzka event',
                        event='crawler_page_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    FilharmoniaLodzPlCrawler().run()


if __name__ == '__main__':
    main()
