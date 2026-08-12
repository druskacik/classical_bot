import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thecumnocktryst.com/'
SOURCE = 'The Cumnock Tryst'
EVENTS_URL = f'{SOURCE_URL}whats-on/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_value(soup, label):
    for element in soup.select('main p.tagline'):
        if clean_text(element).casefold() == label.casefold():
            return clean_text(element.find_next_sibling())
    return ''


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%d %B %Y').date().isoformat()
    except ValueError:
        return None


def parse_times(value):
    times = []
    for hour, minute, meridiem in re.findall(
        r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', value, re.IGNORECASE
    ):
        hour = int(hour)
        if not 1 <= hour <= 12:
            continue
        minute = int(minute or '00')
        if minute > 59:
            continue
        if meridiem.lower() == 'pm' and hour != 12:
            hour += 12
        elif meridiem.lower() == 'am' and hour == 12:
            hour = 0
        times.append(f'{hour:02d}:{minute:02d}')
    return list(dict.fromkeys(times))


def city_for_venue(venue):
    normalized = venue.casefold()
    if 'glasgow royal concert hall' in normalized:
        return 'Glasgow'
    if 'stair church' in normalized:
        return 'Stair'
    # The remaining programme venues are in Cumnock or on the adjacent
    # Dumfries House estate, which the festival presents as a Cumnock venue.
    return 'Cumnock'


def parse_event(soup, url):
    title = clean_text(soup.select_one('main h1')).rstrip('.').strip()
    date_text = labelled_value(soup, 'Date')
    event_date = parse_date(date_text)
    venue = labelled_value(soup, 'Venue')
    if not title or not event_date or not venue:
        return []

    description = clean_text(soup.select_one('main section.component__grid-content')) or None
    times = parse_times(date_text) or [None]
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city_for_venue(venue),
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for time_from in times
    ]


class TheCumnockTrystComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thecumnocktryst_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
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

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(EVENTS_URL, timeout=45)
            response.raise_for_status()
            listing = BeautifulSoup(response.text, 'html.parser')
            urls = list(dict.fromkeys(
                link['href'] for link in listing.select('a.event-card--hero[href]')
                if link['href'].startswith(EVENTS_URL)
            ))
            if not urls:
                raise ValueError('No event detail links found on the programme page')

            records = []
            for url in urls:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                records.extend(parse_event(BeautifulSoup(detail_response.text, 'html.parser'), url))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape The Cumnock Tryst programme',
                event='crawler_fetch_failed',
                level='error',
                url=EVENTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    TheCumnockTrystComCrawler().run()


if __name__ == '__main__':
    main()
