import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operacolumbus.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'events-calendar/')
SOURCE = 'Opera Columbus'
CITY = 'Columbus'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'January February March April May June July August September '
            'October November December'
        ).split(),
        1,
    )
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_cards(soup):
    for button in soup.find_all(
        'a', string=lambda value: value and 'explore' in value.lower()
    ):
        card = button.find_parent(
            'div', class_=lambda value: value and 'three-fourths' in value
        )
        summary = card.find('p') if card else None
        if not summary:
            continue
        lines = [clean_text(line) for line in summary.stripped_strings]
        lines = [line for line in lines if line]
        yield card, lines, urljoin(SOURCE_URL, button.get('href', ''))


def parse_summary(lines):
    date_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(
                r'(?:' + '|'.join(MONTHS) + r')\s+\d{1,2}(?:\s*(?:\+|-)\s*\d{1,2})?,\s*\d{4}',
                line,
            )
        ),
        None,
    )
    if date_index is None or date_index == 0 or date_index + 1 >= len(lines):
        return None
    title = clean_text(' '.join(lines[:date_index]))
    venue = clean_text(lines[date_index + 1])
    match = re.fullmatch(
        r'(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:\s*(?:\+|-)\s*(\d{1,2}))?,\s*(\d{4})',
        lines[date_index],
    )
    if not match or not title or not venue:
        return None
    month, first_day, second_day, year = match.groups()
    days = [first_day] + ([second_day] if second_day else [])
    try:
        dates = [
            date(int(year), MONTHS[month], int(day)).isoformat() for day in days
        ]
    except ValueError:
        return None
    return title, venue, dates


def detail_times(soup, expected_dates):
    article = soup.select_one('main article') or soup.select_one('article')
    text = clean_text(article) if article else ''
    first_section = text[:1500]
    times = {}
    for month, day, time_value, meridiem in re.findall(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?'
        r',?\s*(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
        r'\s*[|│]\s*'
        r'(\d{1,2}(?::\d{2})?)\s*(am|pm)\b',
        first_section,
        flags=re.IGNORECASE,
    ):
        hour_text, _, minute_text = time_value.partition(':')
        hour = int(hour_text)
        minute = int(minute_text or '00')
        if meridiem.lower() == 'pm' and hour != 12:
            hour += 12
        elif meridiem.lower() == 'am' and hour == 12:
            hour = 0
        for expected_date in expected_dates:
            parsed = date.fromisoformat(expected_date)
            if parsed.month == MONTHS[month.title()] and parsed.day == int(day):
                times[expected_date] = f'{hour:02d}:{minute:02d}'
    return text or None, times


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    calendar = get_soup(session, CALENDAR_URL)
    records = []
    for card, lines, url in calendar_cards(calendar):
        parsed = parse_summary(lines)
        if not parsed or not url:
            continue
        title, venue, dates = parsed
        try:
            detail = get_soup(session, url)
            description, times = detail_times(detail, dates)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            description = clean_text(card) or None
            times = {}
        for event_date in dates:
            records.append(
                {
                    'title': title,
                    'date': event_date,
                    'url': url,
                    'time_from': times.get(event_date),
                    'venue': venue,
                    'city': CITY,
                    'country_code': 'US',
                    'description': description,
                    'source_url': SOURCE_URL,
                    'source': SOURCE,
                }
            )
    return sorted(
        records,
        key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ),
    )


class OperaColumbusOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operacolumbus_org',
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
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperaColumbusOrgCrawler().run()


if __name__ == '__main__':
    main()
