import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.schwarzwald-musikfestival.de/'
SOURCE = 'Schwarzwald Musikfestival'
CONCERTS_URL = urljoin(SOURCE_URL, 'Programm/Konzerte')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}

MONTHS = {
    'januar': 1,
    'februar': 2,
    'märz': 3,
    'maerz': 3,
    'april': 4,
    'mai': 5,
    'juni': 6,
    'juli': 7,
    'august': 8,
    'september': 9,
    'oktober': 10,
    'november': 11,
    'dezember': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_time(value):
    match = re.search(
        r'\b(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s+(20\d{2})'
        r'(?:\s*,?\s*(\d{1,2})[.:](\d{2})\s*Uhr)?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4) and int(match.group(4)) < 24:
        event_time = f'{int(match.group(4)):02d}:{match.group(5)}'
    return event_date, event_time


def parse_location(content):
    for paragraph in content.select('p'):
        text = clean_text(paragraph)
        postal = re.search(r'\b\d{5}\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .-]*)$', text)
        if not postal or ',' not in text:
            continue

        city = text.split(',', 1)[0].strip()
        location = text.split(',', 1)[1].strip()
        location = re.sub(
            r',?\s*\d{5}\s+[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß .-]*$',
            '',
            location,
        ).strip(' ,')
        venue = re.sub(
            r'\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.-]*(?:straße|strasse|weg|platz|allee|gasse)'
            r'\s+\d+[A-Za-z]?\s*$',
            '',
            location,
            flags=re.IGNORECASE,
        ).strip(' ,')
        if city and venue:
            return venue, city
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    entry = soup.select_one('.single-post .entry')
    if entry is None:
        return None

    title = clean_text(entry.select_one('.entry-title h2'))
    content = entry.select_one('.entry-content')
    if not title or content is None:
        return None

    event_date, time_from = parse_date_and_time(clean_text(content))
    if not event_date:
        event_date, time_from = parse_date_and_time(title)
    location = parse_location(content)
    if not event_date or not location:
        return None

    description_node = BeautifulSoup(str(content), 'html.parser')
    for unwanted in description_node.select('.ticket-container, script, style'):
        unwanted.decompose()
    description = clean_text(description_node) or None
    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SchwarzwaldMusikfestivalDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='schwarzwald_musikfestival_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CONCERTS_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Schwarzwald Musikfestival concert listing',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        urls = {
            urljoin(SOURCE_URL, link['href'])
            for link in soup.select('a[href*="/konzert/"]')
            if '/de/konzert/' in urljoin(SOURCE_URL, link['href'])
        }
        records = []
        for url in sorted(urls):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Schwarzwald Musikfestival concert detail',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            record = parse_detail(detail_response.text, url)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SchwarzwaldMusikfestivalDeCrawler().run()


if __name__ == '__main__':
    main()
