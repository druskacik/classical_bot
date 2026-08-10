import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivo.de/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programm/')
SOURCE = 'Festivo'
CITY = 'Aschau im Chiemgau'

VENUE_ALIASES = {
    'Preysingsaal Schoß Hohenaschau': 'Preysingsaal Schloss Hohenaschau',
    'Preysingsaal Schloß Hohenaschau': 'Preysingsaal Schloss Hohenaschau',
}

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
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def parse_date(value):
    match = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', value)
    if not match:
        return None
    try:
        return datetime.strptime('.'.join(match.groups()), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*Uhr\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def event_title(frame, description, date):
    paragraph = frame.find('p')
    if paragraph:
        highlighted = paragraph.find(['em', 'strong'])
        title = clean_text(highlighted)
        if title:
            return title

    first_line = next((line for line in description.splitlines() if line), '')
    return first_line or f'Festivo Konzert am {date}'


def parse_programme_page(soup, page_url):
    records = []
    for date_heading in soup.select('main h4'):
        frame = date_heading.find_parent(class_='frame')
        venue_heading = frame.find('h5') if frame else None
        paragraph = frame.find('p') if frame else None
        if not frame or not venue_heading or not paragraph:
            continue

        date = parse_date(clean_text(date_heading))
        venue_and_time = clean_text(venue_heading)
        venue = re.split(r'\s*[•·]\s*', venue_and_time, maxsplit=1)[0].strip()
        venue = VENUE_ALIASES.get(venue, venue)
        description = clean_text(paragraph)
        if not date or not venue or not description:
            continue

        frame_id = frame.get('id')
        url = f'{page_url}#{frame_id}' if frame_id else page_url
        records.append({
            'title': event_title(frame, description, date),
            'date': date,
            'url': url,
            'time_from': parse_time(venue_and_time),
            'venue': venue,
            'city': CITY,
            'country_code': 'DE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    programme_soup = get_soup(session, PROGRAMME_URL)

    page_urls = [PROGRAMME_URL]
    page_urls.extend(
        urljoin(SOURCE_URL, link['href'])
        for link in programme_soup.select('a[href*="/archiv/"]')
        if re.search(r'/archiv/\d{4}/?$', link.get('href', ''))
    )

    records = []
    for page_url in dict.fromkeys(page_urls):
        try:
            soup = programme_soup if page_url == PROGRAMME_URL else get_soup(session, page_url)
            records.extend(parse_programme_page(soup, page_url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Festivo programme page',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {record['url']: record for record in records}
    return sorted(
        unique.values(),
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class FestivoDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivo_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
            'source_url',
            'source',
        ],
        dedupe_subset=['url'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FestivoDeCrawler().run()


if __name__ == '__main__':
    main()
