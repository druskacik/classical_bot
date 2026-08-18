import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.emmanuelmusic.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'Emmanuel Music'
DEFAULT_VENUE = 'Emmanuel Church'
DEFAULT_CITY = 'Boston'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+)?'
    r'([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2})'
    r'(?:\s*(?:,|at)\s*(\d{1,2}(?::\d{2})?\s*[ap]m))?',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '').replace('\u200d', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_time(date_text, time_text=None):
    try:
        normalized_date = re.sub(r'(?<=\d),(?=\s+20\d{2})', '', date_text)
        event_date = datetime.strptime(normalized_date, '%B %d %Y').date().isoformat()
    except ValueError:
        return None, None
    if not time_text:
        return event_date, None
    normalized = re.sub(r'\s+', '', time_text).upper()
    if ':' not in normalized:
        normalized = normalized[:-2] + ':00' + normalized[-2:]
    try:
        event_time = datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')
    except ValueError:
        event_time = None
    return event_date, event_time


def city_for_venue(venue):
    value = venue.lower()
    if any(term in value for term in ('mit', 'tull concert hall')):
        return 'Cambridge'
    if any(term in value for term in ('tufts', 'distler hall')):
        return 'Medford'
    return DEFAULT_CITY


def make_record(title, date_text, time_text, venue, description, url):
    event_date, event_time = parse_date_time(date_text, time_text)
    title = clean_text(title)
    venue = clean_text(venue)
    if not event_date or not title or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': event_time,
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'US',
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def schedule_records(soup, url):
    records = []
    for item in soup.select('.notes-translations-row'):
        date_heading = item.select_one('h2.pre-title-big')
        if not date_heading:
            continue
        date_match = DATE_RE.search(clean_text(date_heading))
        works = [clean_text(node) for node in item.select('h2.header-4')]
        works = [work for work in works if work]
        if not date_match or not works:
            continue
        time_match = re.search(r'\bat\s+(\d{1,2}(?::\d{2})?\s*[ap]m)', clean_text(date_heading), re.I)
        title = ' / '.join(works)
        description = clean_text(item)
        record = make_record(
            title,
            date_match.group(1),
            time_match.group(1) if time_match else '10:00am',
            DEFAULT_VENUE,
            description,
            url,
        )
        if record:
            records.append(record)
    return records


def season_records(soup, url):
    records = []
    for card in soup.select('.mainstage-concert-wrapper'):
        title_node = card.select_one('.event-title')
        date_node = card.select_one('.date-and-time')
        if not title_node or not date_node:
            continue
        text = clean_text(card)
        lines = [line for line in clean_text(date_node).splitlines() if line]
        matches = list(DATE_RE.finditer(clean_text(date_node)))
        detail = card.select_one('a[href*="/performance-info/"]')
        record_url = urljoin(SOURCE_URL, detail.get('href')) if detail else url
        for match in matches:
            following = lines[-1] if lines else DEFAULT_VENUE
            if DATE_RE.search(following) or re.search(r'^March\s+\d+-\d+', following):
                following = DEFAULT_VENUE
            record = make_record(
                title_node,
                match.group(1),
                match.group(2),
                following,
                text,
                record_url,
            )
            if record:
                records.append(record)

    for card in soup.select('.concert-section .section-33 > div'):
        date_node = card.select_one('.text-block-101')
        if not date_node:
            continue
        match = DATE_RE.search(clean_text(date_node))
        if not match:
            continue
        lines = clean_text(date_node).splitlines()
        venue = lines[-1] if len(lines) > 1 else DEFAULT_VENUE
        description = clean_text(card.select_one('.body-text-small'))
        image = card.select_one('img')
        alt = clean_text(image.get('alt')) if image else ''
        if "St. Matthew" in alt:
            title = "J.S. Bach: St. Matthew Passion"
        elif 'opera' in description.lower():
            title = 'Emmanuel Music Holiday Opera and Bach Cantata'
        elif 'Neruda Songs' in description:
            title = "Peter Lieberson: Neruda Songs"
        else:
            title = 'Emmanuel Music Chamber Concert'
        record = make_record(title, match.group(1), match.group(2), venue, description, url)
        if record:
            records.append(record)
    return records


def detail_records(soup, url):
    title_node = soup.select_one('.event-title-header.event-page')
    title = clean_text(title_node) or clean_text(soup.title).removesuffix(' | Emmanuel Music')
    if not title or title in {'Emmanuel Music', 'Performances'}:
        return []
    lines = [line for line in clean_text(soup.body).splitlines() if line]
    records = []
    for index, line in enumerate(lines):
        match = DATE_RE.fullmatch(line.strip())
        if not match:
            continue
        nearby = lines[index + 1:index + 5]
        venue = next(
            (candidate for candidate in nearby if re.search(
                r'(Church|Chapel|Hall|Theatre|Theater|Institut|University|\bMIT\b|Tufts)',
                candidate,
                re.I,
            )),
            DEFAULT_VENUE,
        )
        venue = venue.split('|', 1)[0].strip()
        event_title = title
        event_time = match.group(2)
        if title == 'Lindsey Chapel Series':
            venue = 'Lindsey Chapel, Emmanuel Church'
            event_time = event_time or '12:00pm'
        if 'winterreise' in title.lower():
            prefix = ' '.join(lines[max(0, index - 3):index])
            event_title = 'Schubert/Zender: Winterreise' if 'Zender' in prefix else 'Schubert: Winterreise'
        record = make_record(event_title, match.group(1), event_time, venue, clean_text(soup.body), url)
        if record:
            records.append(record)
    return records


def discover_pages(session):
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()
    sitemap = BeautifulSoup(response.text, 'xml')
    urls = [clean_text(node) for node in sitemap.find_all('loc')]
    excluded = {'emmanuel-digital-program', 'this-week-cantata'}
    return sorted({
        url for url in urls
        if '/performance-info/' in url and url.rstrip('/').rsplit('/', 1)[-1] not in excluded
    })


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    pages = discover_pages(session)
    for url in pages:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            if url.endswith('cantata-schedule'):
                records.extend(schedule_records(soup, url))
            elif url.endswith('performance-season'):
                records.extend(season_records(soup, url))
            else:
                records.extend(detail_records(soup, url))
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Emmanuel Music calendar page',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    if not records:
        log_message(
            'No Emmanuel Music performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class EmmanuelMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='emmanuelmusic_org',
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
    EmmanuelMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
