import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://houstonsymphony.org/'
CALENDAR_URL = f'{SOURCE_URL}performance-calendar/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/performance'
SOURCE = 'Houston Symphony'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'jones-hall': ('Jones Hall', 'Houston'),
    'miller-outdoor-theatre': ('Miller Outdoor Theatre', 'Houston'),
    'the-cynthia-woods-mitchell-pavilion': (
        'The Cynthia Woods Mitchell Pavilion',
        'The Woodlands',
    ),
    'the-hobby-center': ('The Hobby Center', 'Houston'),
}

TICKET_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]{2,3})\.?\s+(\d{1,2})\s+'
    r'(\d{1,2}(?::\d{2})?)\s*([AP])\.?M\.?(?:\s+at\s+(.+))?$',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', html.unescape(str(value)).replace('\xa0', ' ')).strip()


def venue_and_city(value, fallback_slug=''):
    text = clean_text(value)
    lowered = text.lower()
    for slug, (venue, city) in VENUES.items():
        if slug.replace('-', ' ') in lowered or venue.lower() in lowered:
            return venue, city
    return VENUES.get(fallback_slug, ('', ''))


def parse_ticket(value, year):
    match = TICKET_RE.match(clean_text(value))
    if not match:
        return None
    month, day, time_value, meridiem, venue_text = match.groups()
    if month.lower() == 'sept':
        month = 'Sep'
    try:
        event_date = datetime.strptime(
            f'{month} {day} {year}', '%b %d %Y'
        ).date().isoformat()
        time_from = datetime.strptime(
            f'{time_value} {meridiem}M', '%I:%M %p' if ':' in time_value else '%I %p'
        ).strftime('%H:%M')
    except ValueError:
        return None
    return event_date, time_from, venue_text or ''


def description_from_content(soup):
    parts = []
    about_heading = soup.find(
        lambda node: node.name in {'h2', 'h3', 'h4', 'h5'}
        and clean_text(node.get_text(' ', strip=True)).lower() == 'about this concert'
    )
    if about_heading:
        container = about_heading.find_parent(class_='textwidget') or about_heading.parent
        text = clean_text(container.get_text(' ', strip=True))
        text = re.sub(r'^About This Concert\s*', '', text, flags=re.IGNORECASE)
        if text:
            parts.append(text)

    programme = []
    for card in soup.select('.hs-performancePage-programNotes-card'):
        text = clean_text(card.get_text(' ', strip=True))
        if text and text not in programme:
            programme.append(text)
    if programme:
        parts.append('Program:\n' + '\n'.join(programme))
    return '\n\n'.join(parts) or None


def calendar_metadata(soup):
    metadata = {}
    for card in soup.select('.performance-card[data-start-date]'):
        link = card.find('a', href=True)
        start_date = card.get('data-start-date', '')
        if not link or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start_date):
            continue
        url = link['href'].split('#', 1)[0]
        metadata[url] = {
            'year': int(start_date[:4]),
            'venue_slug': card.get('data-venue', ''),
        }
    return metadata


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    calendar_response = session.get(CALENDAR_URL, timeout=60)
    calendar_response.raise_for_status()
    listing = calendar_metadata(BeautifulSoup(calendar_response.text, 'html.parser'))

    api_response = session.get(
        API_URL,
        params={'per_page': 100, 'status': 'publish'},
        timeout=90,
    )
    api_response.raise_for_status()

    records = []
    for item in api_response.json():
        url = item.get('link', '').split('#', 1)[0]
        meta = listing.get(url)
        # The public calendar is the authoritative concrete-performance feed.
        # This also omits translated duplicate and unlisted administrative posts.
        if not meta:
            continue
        title = clean_text(
            BeautifulSoup(
                item.get('title', {}).get('rendered', ''), 'html.parser'
            ).get_text(' ', strip=True)
        )
        if not title:
            continue

        soup = BeautifulSoup(item.get('content', {}).get('rendered', ''), 'html.parser')
        description = description_from_content(soup)
        seen = set()
        for block in soup.select('.hs-performancePage-Ticket-Seperator'):
            parsed = parse_ticket(block.get_text(' ', strip=True), meta['year'])
            if not parsed:
                continue
            event_date, time_from, venue_text = parsed
            venue, city = venue_and_city(venue_text, meta['venue_slug'])
            if not venue or not city:
                continue
            key = (title, event_date, time_from, venue)
            if key in seen:
                continue
            seen.add(key)
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
            'No concrete performances found',
            event='crawler_empty_listing',
            level='warning',
            url=CALENDAR_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'], row['title']))


class HoustonSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='houstonsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        return scrape_concerts()


def main():
    HoustonSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
