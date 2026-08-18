import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chambermusicraleigh.org/'
LISTING_URL = urljoin(SOURCE_URL, 'concerts-2.html')
SOURCE = 'Chamber Music Raleigh'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,.]?\s+'
    r'([A-Z][a-z]+)\s+(\d{1,2}),\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?', re.IGNORECASE)

VENUES = (
    ('Historic Smedes - Emory Parlor, Saint Mary\'s School', 'Raleigh'),
    ('Smedes-Emory Parlor, Saint Mary\'s School', 'Raleigh'),
    ('Smedes-Emory Parlor Saint Mary\'s School', 'Raleigh'),
    ('SECU Auditorium - NC Museum of Art', 'Raleigh'),
    ('North Carolina Museum of Art', 'Raleigh'),
    ('NC Executive Mansion', 'Raleigh'),
    ('North Carolina Governor\'s Mansion', 'Raleigh'),
    ('Wake Forest Renaissance Centre', 'Wake Forest'),
    ('Whitley Auditorium', 'Elon'),
    ('Harold D. Ritter Park Rotary Shelter', 'Cary'),
)

LISTING_DATE_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|October|'
    r'November|December)\s+\d{1,2}(?:-\d{1,2})?,\s+20\d{2}',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ').replace('\u200b', ' ')).strip()


def parse_date(match):
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(match):
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def venue_and_city(text):
    normalized = clean_text(text).lower()
    for venue, city in VENUES:
        if venue.lower() in normalized:
            return venue, city
    return None, None


def occurrence_records(text):
    """Extract concrete dated performances from a detail-page paragraph."""
    records = []
    matches = list(DATE_RE.finditer(text))
    for index, date_match in enumerate(matches):
        event_date = parse_date(date_match)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[date_match.end():end]
        time_match = TIME_RE.search(segment)
        venue, city = venue_and_city(segment)
        if event_date and time_match and venue and city:
            records.append((event_date, parse_time(time_match), venue, city))
    return records


def detail_description(soup):
    parts = []
    for paragraph in soup.select('p'):
        text = clean_text(paragraph.get_text(' ', strip=True))
        if not text or DATE_RE.search(text) or text in parts:
            continue
        if text.upper().startswith(('TICKETS', 'JOIN THE WAIT', 'FREE ADMISSION')):
            continue
        if re.fullmatch(r'[\W\d_]*', text):
            continue
        parts.append(text)
    return '\n\n'.join(parts) or None


def listing_entries(soup):
    entries = []
    paragraphs = soup.select('p')
    for index, paragraph in enumerate(paragraphs):
        ticket_link = next(
            (
                link for link in paragraph.select('a[href]')
                if 'TICKETS' in clean_text(link.get_text(' ', strip=True)).upper()
                and urlparse(urljoin(LISTING_URL, link['href'])).netloc
                == urlparse(SOURCE_URL).netloc
            ),
            None,
        )
        if not ticket_link:
            continue

        context = clean_text(paragraph.get_text(' ', strip=True))
        date_match = DATE_RE.search(context) or LISTING_DATE_RE.search(context)
        if not date_match:
            for nearby in paragraphs[index + 1:index + 3]:
                candidate = clean_text(nearby.get_text(' ', strip=True))
                candidate_match = DATE_RE.search(candidate) or LISTING_DATE_RE.search(candidate)
                if candidate_match:
                    context, date_match = candidate, candidate_match
                    break
        if not date_match:
            continue
        title = clean_text(context[:date_match.start()])
        if title:
            entries.append((title, urljoin(LISTING_URL, ticket_link['href']), context))
    return entries


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(LISTING_URL, timeout=45)
    response.raise_for_status()
    entries = listing_entries(BeautifulSoup(response.text, 'html.parser'))

    records = []
    for title, url, listing_text in entries:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        soup = BeautifulSoup(detail_response.text, 'html.parser')
        description = detail_description(soup)
        detail_text = clean_text(' '.join(
            paragraph.get_text(' ', strip=True) for paragraph in soup.select('p')
        ))
        detail_occurrences = occurrence_records(detail_text)

        occurrences = []
        listing_date_match = DATE_RE.search(listing_text)
        listing_venue, listing_city = venue_and_city(listing_text)
        if listing_date_match and listing_venue and listing_city:
            event_date = parse_date(listing_date_match)
            date_tail = listing_text[listing_date_match.end():]
            for time_match in TIME_RE.finditer(date_tail):
                occurrences.append(
                    (event_date, parse_time(time_match), listing_venue, listing_city)
                )

        # Festival weekends are expanded into their four individually dated
        # recitals on the detail page. The Celtic Ensemble page also publishes
        # concrete tour performances beyond the single calendar occurrence.
        if 'PADEREWSKI PIANO FESTIVAL' in title:
            occurrences = detail_occurrences
        elif 'CLEVELAND CELTIC ENSEMBLE' in title:
            occurrences.extend(detail_occurrences)

        occurrences = list(dict.fromkeys(occurrences))

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

    if not records:
        log_message(
            'No concrete concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class ChamberMusicRaleighOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicraleigh_org',
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
    ChamberMusicRaleighOrgCrawler().run()


if __name__ == '__main__':
    main()
