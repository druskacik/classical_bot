import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://rochestersymphony.org/'
SOURCE = 'Rochester Symphony'
EVENTS_URL = urljoin(SOURCE_URL, 'tickets-and-events')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def season_years(soup):
    text = clean_text(soup)
    match = re.search(r'\b(20\d{2})\s*/\s*(\d{2,4})\s+Season\b', text, re.IGNORECASE)
    if not match:
        raise ValueError('Could not determine season years from events page')
    first_year = int(match.group(1))
    second_part = match.group(2)
    second_year = int(second_part) if len(second_part) == 4 else (first_year // 100) * 100 + int(second_part)
    return first_year, second_year


def parse_performance(value, first_year, second_year):
    text = clean_text(value)
    match = re.search(
        r'\b(' + '|'.join(month for month in (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        )) + r')\s+(\d{1,2})\s*[—–-]\s*(\d{1,2}:\d{2}\s*[ap]m)\b',
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    month, day, time_text = match.groups()
    month_number = datetime.strptime(month[:3], '%b').month
    year = first_year if month_number >= 7 else second_year
    date = datetime(year, month_number, int(day)).date().isoformat()
    time_from = datetime.strptime(time_text.replace(' ', '').upper(), '%I:%M%p').strftime('%H:%M')
    return date, time_from


def detail_description(soup):
    parts = []
    body = soup.select_one('.performance-details .details-txt')
    body_text = clean_text(body)
    if body_text:
        parts.append(body_text)

    program = []
    for note in soup.select('.performance-details .program-note .header'):
        marker = note.select_one('.marker')
        if marker:
            marker.decompose()
        text = clean_text(note)
        if text and text not in program:
            program.append(text)
    if program:
        parts.append('Program:\n' + '\n'.join(program))
    return '\n\n'.join(parts) or None


def scrape_concerts(session=None):
    session = session or requests.Session()
    response = session.get(EVENTS_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    first_year, second_year = season_years(soup)

    descriptions = {}
    records = []
    for event in soup.select('.event-summary'):
        title = clean_text(event.select_one('.content .title h2'))
        link = event.select_one('a[href*="/show-event/"]')
        performance_line = event.select_one('.performance-date-line')
        venue = clean_text(performance_line.select_one('strong')) if performance_line else ''
        performance = parse_performance(performance_line, first_year, second_year)
        if not title or not link or not venue or not performance:
            continue

        url = urljoin(SOURCE_URL, link.get('href', ''))
        if url not in descriptions:
            try:
                detail_response = session.get(url, headers=HEADERS, timeout=60)
                detail_response.raise_for_status()
                descriptions[url] = detail_description(BeautifulSoup(detail_response.text, 'html.parser'))
            except requests.RequestException as error:
                log_message(
                    'Could not retrieve Rochester Symphony event details',
                    event='crawler_detail_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                descriptions[url] = None

        date, time_from = performance
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': 'Rochester',
            'country_code': 'US',
            'description': descriptions[url],
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No Rochester Symphony performances found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class RochesterSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rochestersymphony_org',
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
        return scrape_concerts()


def main():
    RochesterSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
