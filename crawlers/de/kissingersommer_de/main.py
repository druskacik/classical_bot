import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kissingersommer.de/'
SOURCE = 'Kissinger Sommer'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programm/konzerte/index.html')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def programme_year(soup):
    for element in soup.select('[title], a'):
        text = f"{element.get('title', '')} {clean_text(element)}"
        match = re.search(r'Programme?\s*&\s*Gäste\s+(20\d{2})', text, re.I)
        if match:
            return int(match.group(1))
    raise ValueError('Could not determine the programme year')


def parse_date(value, year):
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.', value)
    if not match:
        return None
    try:
        return date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def detail_description(soup):
    sections = []
    info = clean_text(soup.select_one('.concert-info'))
    if info:
        sections.append(info)

    extras = soup.select_one('.concert-extras')
    if extras:
        # Ticket prices and purchase controls are unrelated to programme extraction.
        for element in extras.select('.tickets, .ticket, .prices, .price, a, button'):
            element.decompose()
        text = clean_text(extras)
        text = re.split(r'\nTickets und Pakete\b', text, maxsplit=1)[0].strip()
        if text:
            sections.append(text)
    return '\n\n'.join(sections) or None


def parse_event(item, detail_soup, year):
    link = item.select_one(':scope > a[href*="ev[id]"]')
    title = clean_text(item.select_one('.title'))
    event_date = parse_date(clean_text(item.select_one('.day')), year)
    venue = clean_text(item.select_one('.location'))
    if not link or not title or not event_date or not venue:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(PROGRAMME_URL, link['href']),
        'time_from': parse_time(clean_text(item.select_one('.time'))),
        'venue': venue,
        'city': 'Bad Kissingen',
        'country_code': 'DE',
        'description': detail_description(detail_soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class KissingerSommerDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kissingersommer_de',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(PROGRAMME_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Kissinger Sommer programme',
                event='crawler_fetch_failed',
                level='error',
                url=PROGRAMME_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        year = programme_year(soup)
        records = []
        for item in soup.select('.item'):
            link = item.select_one(':scope > a[href*="ev[id]"]')
            if link is None:
                continue
            detail_url = urljoin(PROGRAMME_URL, link['href'])
            try:
                detail_response = session.get(detail_url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Kissinger Sommer concert detail',
                    event='crawler_detail_fetch_failed',
                    level='warning',
                    url=detail_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
            record = parse_event(item, detail_soup, year)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KissingerSommerDeCrawler().run()


if __name__ == '__main__':
    main()
