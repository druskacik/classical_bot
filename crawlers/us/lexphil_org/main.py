import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lexphil.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Lexington Philharmonic'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+'
    r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})'
)
TIME_RE = re.compile(r'(\d{1,2}):([0-5]\d)\s*([AP]M)', re.I)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_sections(listing):
    sections = []
    seen = set()
    for heading in listing.select('article h2'):
        section = heading.find_parent('section')
        if not section:
            continue
        link = next(
            (a for a in section.select('a[href]') if clean_text(a) == 'Learn More'),
            None,
        )
        if not link:
            continue
        url = urljoin(EVENTS_URL, link['href'])
        if url in seen:
            continue
        seen.add(url)
        sections.append((clean_text(heading), url, section))
    return sections


def parse_date(value):
    match = DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'PM' else 0)
    return f'{hour:02d}:{match.group(2)}'


def detail_paragraphs(detail):
    return [
        ' | '.join(clean_text(part) for part in paragraph.stripped_strings)
        for paragraph in detail.select('article p')
        if clean_text(paragraph)
    ]


def standard_location(paragraphs):
    for index, text in enumerate(paragraphs[:-1]):
        if text.strip().lower() == 'venue':
            parts = [clean_text(part) for part in paragraphs[index + 1].split('|')]
            parts = [part for part in parts if part]
            if not parts:
                return None, None
            city = None
            for part in parts[1:]:
                match = re.match(r'(.+?),\s*(?:Kentucky|KY)\b', part, re.I)
                if match:
                    city = clean_text(match.group(1))
                    break
            return parts[0], city
    return None, None


def description_from_detail(paragraphs):
    excluded = {
        'date', 'dates & times', 'time', 'venue', 'duration', 'student tickets',
        'learn more', 'subscribe & save', 'single tickets', 'buy tickets',
        'getting here', 'directions', 'parking', 'lextran', 'nearby dining',
        'when you’re here', 'ticketing policies', 'map & seating chart',
        'accessibility', 'health & safety info', 'digital tickets',
    }
    useful = [text for text in paragraphs if text.strip().lower() not in excluded]
    return '\n\n'.join(useful) or None


def tour_records(title, url, section, paragraphs, description):
    records = []
    for text in paragraphs:
        event_date = parse_date(text)
        time_from = parse_time(text)
        parts = [clean_text(part) for part in text.split('|') if clean_text(part)]
        if not event_date or not time_from or len(parts) < 4:
            continue
        city_match = re.match(r'(.+?),\s*(?:Kentucky|KY)\b', parts[-1], re.I)
        venue = parts[-2]
        city = clean_text(city_match.group(1)) if city_match else None
        if venue and city:
            records.append(make_record(title, event_date, time_from, venue, city, url, description))
    return records


def make_record(title, event_date, time_from, venue, city, url, description):
    return {
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
    }


def parse_event(title, url, section, detail):
    paragraphs = detail_paragraphs(detail)
    description = description_from_detail(paragraphs)
    tour = tour_records(title, url, section, paragraphs, description)
    if tour:
        return tour

    venue, city = standard_location(paragraphs)
    if not venue or not city:
        return []

    records = []
    seen = set()
    for paragraph in section.select('p'):
        text = clean_text(paragraph)
        event_date = parse_date(text)
        if not event_date:
            continue
        time_from = parse_time(text)
        key = (event_date, time_from)
        if key not in seen:
            seen.add(key)
            records.append(
                make_record(title, event_date, time_from, venue, city, url, description)
            )
    return records


class LexphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lexphil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        listing = fetch_soup(session, EVENTS_URL)
        records = []
        for title, url, section in event_sections(listing):
            try:
                detail = fetch_soup(session, url)
                records.extend(parse_event(title, url, section, detail))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    LexphilOrgCrawler().run()


if __name__ == '__main__':
    main()
