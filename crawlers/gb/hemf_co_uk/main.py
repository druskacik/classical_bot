import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hemf.co.uk/'
SOURCE = 'Hastings Chamber Music Festival'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'
CITY = 'St Leonards-on-Sea'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_LINE_RE = re.compile(
    r'^(?P<time>\d{1,2}(?:(?:[:.]\d{2}))?\s*(?:am|pm))\s+'
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+)?'
    r'(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+'
    r'(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<venue>.+)$',
    re.IGNORECASE,
)
PROGRAMME_TITLE_RE = re.compile(r'^Programme\s+(20\d{2})$', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalise_time(value):
    parsed = datetime.strptime(value.replace('.', ':').replace(' ', '').upper(), '%I%p')
    if ':' in value or '.' in value:
        parsed = datetime.strptime(value.replace('.', ':').replace(' ', '').upper(), '%I:%M%p')
    return parsed.strftime('%H:%M')


def normalise_venue(value):
    venue = re.sub(r'\s+', ' ', value).strip(' ,.')
    if venue.lower() in {'christchurch', 'christ church'}:
        return 'Christ Church'
    if venue.lower() == 'kino teatr':
        return 'Kino Teatr'
    return venue


def event_title(lines, date_index):
    if not date_index:
        return None
    title = lines[date_index - 1].strip()
    return title if title and len(title) <= 100 else None


def parse_programme(content, url, year):
    soup = BeautifulSoup(content, 'html.parser')
    main = soup.select_one('article') or soup.select_one('main') or soup.body
    if not main:
        return []

    lines = [line.strip() for line in clean_text(main).splitlines() if line.strip()]
    matches = [(index, DATE_LINE_RE.match(line)) for index, line in enumerate(lines)]
    matches = [(index, match) for index, match in matches if match]
    records = []

    for position, (date_index, match) in enumerate(matches):
        next_index = matches[position + 1][0] if position + 1 < len(matches) else len(lines)
        title = event_title(lines, date_index)
        venue = normalise_venue(match.group('venue'))
        month = match.group('month')
        if month.lower() == 'sept':
            month = 'Sep'
        try:
            event_date = datetime.strptime(
                f"{match.group('day')} {month} {year}", '%d %b %Y'
            ).date().isoformat()
        except ValueError:
            try:
                event_date = datetime.strptime(
                    f"{match.group('day')} {month} {year}", '%d %B %Y'
                ).date().isoformat()
            except ValueError:
                continue
        if not title or not venue:
            continue

        description_lines = lines[date_index + 1:next_index]
        for index, line in enumerate(description_lines):
            if line.lower() in {'buy tickets', 'tickets coming soon'}:
                description_lines = description_lines[:index]
                break
        description = '\n'.join(description_lines).strip() or None
        records.append(
            {
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': normalise_time(match.group('time')),
                'venue': venue,
                'city': CITY,
                'country_code': 'GB',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            }
        )
    return records


def programme_pages(session):
    response = session.get(
        PAGES_API,
        params={'per_page': 100, '_fields': 'link,title'},
        timeout=45,
    )
    response.raise_for_status()
    pages = []
    for page in response.json():
        title = clean_text(BeautifulSoup(page.get('title', {}).get('rendered', ''), 'html.parser'))
        match = PROGRAMME_TITLE_RE.match(title)
        if match:
            pages.append((page['link'], int(match.group(1))))
    return pages


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url, year in programme_pages(session):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_programme(response.content, url, year))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape HCMF programme page',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class HemfCoUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hemf_co_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    HemfCoUkCrawler().run()


if __name__ == '__main__':
    main()
