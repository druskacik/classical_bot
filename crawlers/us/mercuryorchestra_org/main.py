import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mercuryorchestra.org/'
SOURCE = 'Mercury Orchestra'
CONCERTS_URL = f'{SOURCE_URL}concerts.html'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'\b(?:MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s*'
    r'([A-Z]+\s+\d{1,2},\s+20\d{2})\s*\.?\s*'
    r'(\d{1,2}:\d{2})\s*([AP]M)\b',
    re.IGNORECASE,
)

VENUES = (
    ('LOCATION CHANGE:', 'Jordan Hall, New England Conservatory', 'Boston'),
    ('Jordan Hall', 'Jordan Hall, New England Conservatory', 'Boston'),
    ('Hatch Memorial Shell', 'Hatch Memorial Shell on the Esplanade', 'Boston'),
    ('Kresge Auditorium', 'Kresge Auditorium', 'Cambridge'),
    ('First Church of Cambridge', 'First Church of Cambridge', 'Cambridge'),
    ('Sanders Theatre', 'Sanders Theatre, Harvard University', 'Cambridge'),
)


def clean_lines(element):
    return [
        re.sub(r'\s+', ' ', line).strip()
        for line in element.get_text('\n', strip=True).splitlines()
        if re.sub(r'\s+', ' ', line).strip()
    ]


def parse_event(cell):
    heading = cell.select_one('p.orange-heading')
    if heading is None:
        return None

    title = re.sub(r'\s+', ' ', heading.get_text(' ', strip=True)).strip()
    lines = clean_lines(cell)
    text = '\n'.join(lines)
    date_match = DATE_TIME_RE.search(text)
    if not title or not date_match:
        return None

    try:
        event_date = datetime.strptime(
            date_match.group(1).title(), '%B %d, %Y'
        ).date().isoformat()
        time_from = datetime.strptime(
            f'{date_match.group(2)} {date_match.group(3).upper()}', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None

    venue = city = None
    for marker, candidate_venue, candidate_city in VENUES:
        if marker.casefold() in text.casefold():
            venue, city = candidate_venue, candidate_city
            break
    if not venue or not city:
        return None

    description_lines = [line for line in lines if line != title and not DATE_TIME_RE.search(line)]
    description = '\n'.join(description_lines) or None

    return {
        'title': title,
        'date': event_date,
        'url': CONCERTS_URL,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': description,
    }


class MercuryOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mercuryorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Mercury Orchestra concert archive',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for heading in soup.select('p.orange-heading'):
            cell = heading.find_parent('td')
            if cell is None:
                continue
            record = parse_event(cell)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title']
            ),
        )


def main():
    MercuryOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
