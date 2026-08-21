import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://grazianodurso.it/'
SOURCE = "Graziano D'Urso"
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (compatible; ClassicalBot/1.0; '
        '+https://github.com/stanislavseres/classical-music-web-scraping)'
    ),
}

EVENT_PATTERN = re.compile(
    r'(?:^|\n)\s*\d+\s*-\s*'
    r'(?P<date>\d{2}/\d{2}/\d{4})\s*-\s*'
    r'(?P<description>.*?)'
    r'(?=\n\s*\d+\s*-\s*\d{2}/\d{2}/\d{4}\s*-|\n\s*Per restare|\Z)',
    re.DOTALL,
)
VENUE_CITY_PATTERN = re.compile(
    r'\bpresso\s+(?:(?:il|la|lo|l[’\'])\s+)?'
    r'(?P<venue>.+?)\s+(?:di|a)\s+'
    r'(?P<city>[A-ZÀ-ÖØ-Ý][A-Za-zÀ-ÖØ-öø-ÿ’\' -]+?)'
    r'(?=\s*[-,.]|\s+nel\b|\s+in\b|$)',
    re.IGNORECASE,
)


def clean_text(value):
    return re.sub(r'\s+', ' ', value).strip()


def parse_event(event_date, description):
    description = clean_text(description)
    location = VENUE_CITY_PATTERN.search(description)
    if not location:
        return None

    try:
        parsed_date = datetime.strptime(event_date, '%d/%m/%Y').date().isoformat()
    except ValueError:
        return None

    venue = clean_text(location.group('venue')).strip(' -,.')
    city = clean_text(location.group('city')).strip(' -,.')
    title = clean_text(description[:location.start()]).strip(' -,.')
    if not all((title, venue, city)):
        return None

    return {
        'title': title,
        'date': parsed_date,
        'url': SOURCE_URL,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': 'IT',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_events(html):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('main')
    if article is None:
        return []

    text = article.get_text('\n', strip=True)
    marker = 'Eventi in programma'
    if marker not in text:
        return []
    event_text = text.split(marker, 1)[1]

    records = []
    for match in EVENT_PATTERN.finditer(event_text):
        record = parse_event(match.group('date'), match.group('description'))
        if record:
            records.append(record)
        else:
            log_message(
                'Skipped Graziano D\'Urso event with incomplete location data',
                event='crawler_item_skipped',
                level='warning',
                url=SOURCE_URL,
            )
    return records


class GrazianoDursoItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='grazianodurso_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Graziano D\'Urso events',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_events(response.content)
        return sorted(records, key=lambda row: (row['date'], row['title'], row['venue']))


def main():
    GrazianoDursoItCrawler().run()


if __name__ == '__main__':
    main()
