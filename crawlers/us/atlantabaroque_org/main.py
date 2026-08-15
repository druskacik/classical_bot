import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://atlantabaroque.org/'
SEASON_URL = urljoin(SOURCE_URL, '28th-season')
SOURCE = 'Atlanta Baroque Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

VENUE_CITIES = {
    'the cathedral of st. philip': 'Atlanta',
    'cathedral of st. philip': 'Atlanta',
    'lassiter concert hall': 'Marietta',
    'callanwolde fine arts center': 'Atlanta',
    'first presbyterian church': 'Atlanta',
    'johns creek united methodist church': 'Johns Creek',
}

DATE_LINE_RE = re.compile(
    r'^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*'
    r'(?P<time>\d{1,2}:\d{2})\s*(?P<meridiem>[ap])\.?m\.?\s*\|\s*'
    r'(?P<venue>.+)$',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element if isinstance(element, str) else element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_years(soup):
    heading = clean_text(soup.find('h1'))
    match = re.search(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\b', heading)
    if not match:
        raise ValueError('Could not determine years from the season heading')
    return int(match.group(1)), int(match.group(2))


def parse_time(value, meridiem):
    hour, minute = (int(part) for part in value.split(':'))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if meridiem.lower() == 'p' and hour != 12:
        hour += 12
    elif meridiem.lower() == 'a' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def parse_occurrence(value, start_year, end_year):
    value = re.sub(r'\s+', ' ', clean_text(value)).strip()
    match = DATE_LINE_RE.match(value)
    if not match:
        return None

    month = MONTHS.get(match.group('month').lower())
    venue = re.sub(r'\s*,\s*[A-Za-z .]+$', '', match.group('venue')).strip()
    city = VENUE_CITIES.get(venue.lower())
    time_from = parse_time(match.group('time'), match.group('meridiem'))
    if not month or not city or not time_from:
        return None

    year = start_year if month >= 7 else end_year
    try:
        event_date = date(year, month, int(match.group('day'))).isoformat()
    except ValueError:
        return None
    return event_date, time_from, venue, city


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    heading = soup.find('h1')
    if heading is None:
        return None

    section = heading.find_parent('section') or heading.parent
    parts = []
    for element in section.select('h4, p'):
        text = clean_text(element)
        if not text or DATE_LINE_RE.match(re.sub(r'\s+', ' ', text)):
            continue
        if text.lower().startswith(('ticket sales', 'praise for')):
            continue
        if text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


def card_description(card):
    parts = []
    for paragraph in card.find_all('p'):
        text = clean_text(paragraph)
        if not text or DATE_LINE_RE.match(re.sub(r'\s+', ' ', text)):
            continue
        if text.lower().startswith(('ticket sales', 'praise for')):
            continue
        if text not in parts:
            parts.append(text)
    return clean_text('\n\n'.join(parts)) or None


class AtlantaBaroqueOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='atlantabaroque_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SEASON_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Atlanta Baroque Orchestra season',
                event='crawler_fetch_failed',
                level='error',
                url=SEASON_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        start_year, end_year = season_years(soup)
        records = []
        for card in soup.select('div[data-ux="ContentCard"]'):
            heading = card.find('h4')
            occurrences = [
                occurrence
                for paragraph in card.find_all('p')
                if (occurrence := parse_occurrence(
                    clean_text(paragraph), start_year, end_year
                ))
            ]
            if heading is None or not occurrences:
                continue

            title = clean_text(heading)
            detail_link = card.find('a', href=True, string=re.compile(r'learn more', re.I))
            url = urljoin(SEASON_URL, detail_link['href']) if detail_link else SEASON_URL
            description = None
            if detail_link:
                try:
                    description = detail_description(session, url)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Atlanta Baroque Orchestra concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
            description = description or card_description(card)

            for event_date, time_from, venue, city in occurrences:
                records.append({
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': time_from,
                    'venue': venue,
                    'city': city,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                })

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    AtlantaBaroqueOrgCrawler().run()


if __name__ == '__main__':
    main()
