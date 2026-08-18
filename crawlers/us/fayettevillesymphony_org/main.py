import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fayettevillesymphony.org/'
SOURCE = 'Fayetteville Symphony Orchestra'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
DETAIL_URLS = [
    f'{SOURCE_URL}current-season/',
    f'{SOURCE_URL}family-and-community/',
    f'{SOURCE_URL}student-concerts/',
    f'{SOURCE_URL}fayetteville-symphonic-band/',
]
CITY = 'Fayetteville'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        ('', 'January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December')
    )
    if month
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def normalized_words(value):
    ignored = {'concert', 'conducts', 'with', 'the', 'fso', 'fayetteville'}
    return {
        word for word in re.findall(r'[a-z0-9]+', clean_text(value).lower())
        if len(word) > 2 and word not in ignored
    }


def season_years(soup):
    heading = soup.find(['h1', 'h2'], string=lambda value: value and 'Season' in value)
    match = re.search(r'(20\d{2})\D+(20\d{2})', clean_text(heading))
    if not match:
        raise ValueError('Could not determine the calendar season years')
    return int(match.group(1)), int(match.group(2))


def parse_calendar_date(value, first_year, second_year):
    match = re.search(r'([A-Za-z]+)\s+(\d{1,2})\b', clean_text(value))
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if not month:
        return None
    year = first_year if month >= 7 else second_year
    try:
        return datetime(year, month, int(match.group(2))).date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    text = clean_text(value).lower().replace('.', '')
    matches = re.findall(r'(\d{1,2}(?::\d{2})?\s*[ap]m)', text)
    if not matches:
        return None
    # When doors and concert times are both listed, the performance is last.
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(matches[-1].replace(' ', ''), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def detail_sections(soup, url):
    sections = []
    for heading in soup.select('.text h3'):
        title = clean_text(heading.get_text(' ', strip=True))
        parts = []
        for sibling in heading.find_next_siblings():
            if sibling.name in {'h1', 'h2', 'h3'}:
                break
            if sibling.name == 'p':
                text = clean_text(sibling.get_text(' ', strip=True))
                if text and not re.search(r'\b20\d{2}\b.*\|', text):
                    parts.append(text)
        sections.append((title, '\n\n'.join(parts) or None, url))
    return sections


def best_detail(title, sections):
    if 'symphonic band' in title.lower():
        return (None, f'{SOURCE_URL}fayetteville-symphonic-band/')
    wanted = normalized_words(title)
    best = None
    best_score = 0
    for detail_title, description, url in sections:
        score = len(wanted & normalized_words(detail_title))
        if score > best_score:
            best = (description, url)
            best_score = score
    return best if best_score else (None, CALENDAR_URL)


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    responses = {}
    for url in [CALENDAR_URL, *DETAIL_URLS]:
        response = session.get(url, timeout=45)
        response.raise_for_status()
        responses[url] = BeautifulSoup(response.text, 'html.parser')

    first_year, second_year = season_years(responses[DETAIL_URLS[0]])
    sections = []
    for url in DETAIL_URLS:
        sections.extend(detail_sections(responses[url], url))

    records = []
    for row in responses[CALENDAR_URL].select('.text table tr'):
        cells = [clean_text(cell.get_text(' ', strip=True)) for cell in row.find_all('td')]
        if len(cells) != 3:
            continue
        title, date_and_time, venue = cells
        if re.search(r'\b(camp|auditions?)\b', title, re.IGNORECASE):
            continue
        event_date = parse_calendar_date(date_and_time, first_year, second_year)
        if not title or not event_date or not venue:
            continue
        description, url = best_detail(title, sections)
        records.append({
            'title': title.rstrip('*†‡ '),
            'date': event_date,
            'url': url,
            'time_from': parse_time(date_and_time),
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No concert rows found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FayettevilleSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fayettevillesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FayettevilleSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
