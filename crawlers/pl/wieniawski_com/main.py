import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wieniawski.com/'
SCHEDULE_URL = 'https://www.wieniawski.pl/harmonogram_wieniawski2026.html'
DETAILS_URL = 'https://www.wieniawski.pl/bilety_wieniawski2026.html'
SOURCE = 'Towarzystwo Muzyczne im. Henryka Wieniawskiego w Poznaniu'
DEFAULT_VENUE = 'Aula Uniwersytecka'
DEFAULT_CITY = 'Poznań'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1,
    'lutego': 2,
    'marca': 3,
    'kwietnia': 4,
    'maja': 5,
    'czerwca': 6,
    'lipca': 7,
    'sierpnia': 8,
    'września': 9,
    'października': 10,
    'listopada': 11,
    'grudnia': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def expand_days(day_text):
    numbers = [int(value) for value in re.findall(r'\d{1,2}', day_text)]
    if len(numbers) == 1:
        return numbers
    if len(numbers) == 2 and re.search(r'[-–]', day_text):
        return list(range(numbers[0], numbers[1] + 1))
    return numbers


def parse_schedule(html, details_html=None):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#content')
    if not content:
        return []

    details = (
        BeautifulSoup(details_html, 'html.parser').select_one('#content')
        if details_html else None
    )
    description = clean_text(details or content) or None
    lines = [line.strip() for line in clean_text(content).splitlines() if line.strip()]
    year_match = re.search(r'\b(20\d{2})\b', description or '')
    if not year_match:
        return []
    year = int(year_match.group(1))

    records = []
    pattern = re.compile(
        r'^(\d{1,2}(?:\s*[-–]\s*\d{1,2})?)\s+'
        r'(' + '|'.join(MONTHS) + r')\s*:\s*(.+)$',
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        title = match.group(3).strip()
        if re.search(r'\bdzień wolny\b', title, re.IGNORECASE):
            continue
        month = MONTHS[match.group(2).lower()]
        for day in expand_days(match.group(1)):
            try:
                event_date = date(year, month, day).isoformat()
            except ValueError:
                continue

            is_warsaw = bool(re.search(r'\bWarszaw', title, re.IGNORECASE))
            records.append({
                'title': title,
                'date': event_date,
                'url': SCHEDULE_URL,
                'time_from': None,
                'venue': 'Filharmonia Narodowa' if is_warsaw else DEFAULT_VENUE,
                'city': 'Warszawa' if is_warsaw else DEFAULT_CITY,
                'country_code': 'PL',
                'description': description,
            })

    unique = {(row['title'], row['date'], row['venue']): row for row in records}
    return sorted(unique.values(), key=lambda row: (row['date'], row['title']))


class WieniawskiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wieniawski_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        try:
            response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
            details_response = requests.get(DETAILS_URL, headers=HEADERS, timeout=45)
            details_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to scrape competition schedule',
                event='crawler_page_failed',
                level='error',
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = parse_schedule(response.text, details_response.text)
        log_message(
            'Competition schedule parsed',
            event='crawler_page_parsed',
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return records


def main():
    WieniawskiComCrawler().run()


if __name__ == '__main__':
    main()
