import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ensembleintercontemporain.com/'
AGENDA_URL = 'https://www.ensembleintercontemporain.com/fr/agenda/'
SOURCE = 'Ensemble intercontemporain'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}
EVENT_RE = re.compile(r'-(\d{4}-\d{2}-\d{2})-(\d{1,2})h(\d{2})(?:-|/|$)')

VENUE_CITIES = {
    'cité de la musique': 'Paris',
    'conservatoire de paris': 'Paris',
    'fondation fiminco - romainville': 'Romainville',
    'fondation louis vuitton': 'Paris',
    'ircam': 'Paris',
    'maison de la radio et de la musique': 'Paris',
    'munich residence': 'Munich',
    'philharmonie de paris': 'Paris',
    'université de york': 'York',
}

COUNTRY_BY_CITY = {
    # Belgium
    'bruges': 'BE', 'charleroi': 'BE', 'gand': 'BE', 'hasselt': 'BE',
    # Romania
    'bucarest': 'RO', 'timisoara': 'RO', 'timișoara': 'RO',
    # Germany
    'baden-baden': 'DE', 'cologne': 'DE', 'dresde': 'DE', 'munich': 'DE',
    # Denmark
    'copenhague': 'DK', 'odense': 'DK',
    # Italy
    'crémone': 'IT', 'milan': 'IT', 'rome': 'IT',
    # Other tour countries
    'genève': 'CH', 'lisbonne': 'PT', 'londres': 'GB', 'luxembourg': 'LU',
    'wroclaw': 'PL', 'séoul': 'KR', 'tongyeong': 'KR', 'tokyo': 'JP',
    'york': 'GB',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    lines = [' '.join(line.replace('\xa0', ' ').split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def canonical_url(value):
    parts = urlsplit(urljoin(AGENDA_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def location_from_card(card):
    location = card.select_one('.location')
    raw_city = clean_text(location.select_one('.city') if location else None)
    raw_place = clean_text(location.select_one('.place') if location else None)
    if not raw_city:
        return None, None, None

    normalized = raw_city.casefold()
    if normalized in VENUE_CITIES:
        city = VENUE_CITIES[normalized]
        venue = ' — '.join(part for part in (raw_city, raw_place) if part)
    else:
        city = 'Paris' if normalized == 'paris' else raw_city
        venue = raw_place

    if not venue:
        return None, None, None
    country_code = COUNTRY_BY_CITY.get(city.casefold(), 'FR')
    return city, venue, country_code


def parse_card(card):
    url = canonical_url(card.get('href'))
    match = EVENT_RE.search(url)
    title = clean_text(card.select_one('.title'))
    city, venue, country_code = location_from_card(card)
    if not match or not title or not city or not venue:
        return None
    return {
        'title': title,
        'date': match.group(1),
        'url': url,
        'time_from': f'{int(match.group(2)):02d}:{match.group(3)}',
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    parts = []

    sidebar_sections = soup.select('main aside.red > section')
    if len(sidebar_sections) > 1:
        programme = clean_text(sidebar_sections[1])
        if programme:
            parts.append(programme)

    body = clean_text(soup.select_one('main > section.content, main section.content'))
    if body and body not in parts:
        parts.append(body)
    return '\n\n'.join(parts) or None


class EnsembleIntercontemporainComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ensembleintercontemporain_com',
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
        response = session.get(AGENDA_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        archive_urls = {
            urljoin(AGENDA_URL, link.get('href'))
            for link in soup.select('a[href*="season="]')
            if link.get('href')
        }
        page_urls = [AGENDA_URL, *sorted(archive_urls)]
        records_by_key = {}

        for page_url in page_urls:
            page_response = session.get(page_url, timeout=45)
            page_response.raise_for_status()
            page = BeautifulSoup(page_response.text, 'html.parser')
            for card in page.select('a.item-content[href*="/concert/"]'):
                record = parse_card(card)
                if not record:
                    log_message(
                        'Skipped incomplete Ensemble intercontemporain performance',
                        event='crawler_item_skipped',
                        level='warning',
                        url=canonical_url(card.get('href')) or page_url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, or city is missing',
                    )
                    continue
                key = (record['url'], record['date'], record['time_from'], record['venue'])
                records_by_key[key] = record

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(detail_description, session, record['url']): record
                for record in records_by_key.values()
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Could not enrich Ensemble intercontemporain performance',
                        event='crawler_detail_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records_by_key.values(),
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    EnsembleIntercontemporainComCrawler().run()


if __name__ == '__main__':
    main()
