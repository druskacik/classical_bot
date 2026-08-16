import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://musicmountain.org/'
SOURCE = 'Music Mountain'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
VENUE = 'Gordon Hall, Music Mountain'
CITY = 'Falls Village'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_HEADING_RE = re.compile(
    r'^(?P<month>JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|'
    r'SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+'
    r'(?P<day>\d{1,2})(?:\s+at\s+(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M))?',
    re.IGNORECASE | re.MULTILINE,
)


def clean_text(value):
    if value is None:
        return ''
    if isinstance(value, Tag):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )
    session.mount('https://', HTTPAdapter(max_retries=retry))
    return session


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    parser = 'xml' if url.endswith('.xml') else 'html.parser'
    return BeautifulSoup(response.text, parser)


def parse_time(value):
    match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([AP])M', value, re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.upper() == 'P' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def is_date_heading(tag):
    return (
        isinstance(tag, Tag)
        and tag.name == 'strong'
        and DATE_HEADING_RE.match(clean_text(tag)) is not None
    )


def following_description(heading):
    parts = []
    for node in heading.next_elements:
        if node is heading:
            continue
        if is_date_heading(node):
            break
        if isinstance(node, NavigableString):
            # Heading text is already used for the title, so do not repeat it.
            if heading in node.parents:
                continue
            value = clean_text(node)
            if value:
                parts.append(value)
    return clean_text('\n'.join(parts)) or None


def parse_season_page(soup, url, year):
    records = []
    seen_headings = set()
    for heading in soup.find_all('strong'):
        signature = clean_text(heading)
        matches = list(DATE_HEADING_RE.finditer(signature))
        if not matches:
            continue

        # Nested strong elements can expose the same visible heading twice.
        if signature in seen_headings:
            continue
        seen_headings.add(signature)
        description = following_description(heading)
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(signature)
            title = clean_text(signature[match.end():end]).strip(' -–—:,.')
            title = re.sub(
                r'^\(?\s*(?:Sunday|Saturday|Tuesday)(?:\s+of\s+Labor\s+Day\s+Weekend)?'
                r'\s+at\s+\d{1,2}(?::\d{2})?\s*[AP]M\s*\)?\s*',
                '',
                title,
                flags=re.IGNORECASE,
            ).strip()
            # A date-only announcement (for example a newsletter deadline) is
            # not a concrete performance and has no usable event title.
            if not title or re.match(r'^\d{1,2}(?:\s*,\s*\d{1,2})+', title):
                continue

            try:
                event_date = datetime.strptime(
                    f"{match.group('month')} {match.group('day')} {year}", '%B %d %Y'
                ).date()
            except ValueError:
                continue

            time_from = parse_time(match.group('time') or '')
            if time_from is None:
                # The season overview states that this series is Sundays at 3 PM
                # and Saturdays at 7 PM; the calendar date resolves the series.
                time_from = '15:00' if event_date.weekday() == 6 else '19:00'

            records.append({
                'title': title,
                'date': event_date.isoformat(),
                'url': url,
                'time_from': time_from,
                'venue': VENUE,
                'city': CITY,
                'description': description,
            })
    return records


def parse_listing_page(soup):
    records = []
    for card in soup.select('.shows--entry-block'):
        title_node = card.select_one('.show-title')
        date_node = card.select_one('.show-date')
        venue_node = card.select_one('.show-venue, .venue-title')
        link = card.select_one('a[href*="/show-details/"]')
        if not title_node or not date_node or not link:
            continue
        match = re.search(
            r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})(?:\s*-\s*(\d{1,2}(?::\d{2})?\s*[AP]M))?',
            clean_text(date_node),
        )
        if not match:
            continue
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        except ValueError:
            continue
        description_node = card.select_one('.shows--entry-content')
        records.append({
            'title': clean_text(title_node),
            'date': event_date,
            'url': urljoin(SOURCE_URL, link.get('href')),
            'time_from': parse_time(match.group(2) or ''),
            'venue': clean_text(venue_node) or VENUE,
            'city': CITY,
            'description': clean_text(description_node) or None,
        })
    return records


class MusicmountainOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicmountain_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        try:
            sitemap = fetch_soup(session, SITEMAP_URL)
            urls = [clean_text(node) for node in sitemap.select('loc')]
            season_urls = sorted({
                url for url in urls
                if re.search(r'/season-overview-(\d{4})/?$', url)
            })
            if not season_urls:
                raise ValueError('No season overview was found in the sitemap')

            records = []
            for season_url in season_urls:
                year = int(re.search(r'(\d{4})/?$', season_url).group(1))
                records.extend(parse_season_page(fetch_soup(session, season_url), season_url, year))

            # The category pages provide canonical detail URLs for upcoming
            # events. Category 15 is chamber music; category 2 is the mixed
            # Saturday Jazz/Audience Favorites series. Both are needed for
            # comprehensive scope coverage.
            listing_records = []
            for listing_url in (
                urljoin(SOURCE_URL, 'chamber_concerts'),
                urljoin(SOURCE_URL, 'saturday-jazz'),
            ):
                listing_records.extend(parse_listing_page(fetch_soup(session, listing_url)))

            listing_by_key = {
                (record['title'].casefold(), record['date']): record
                for record in listing_records
            }
            for record in records:
                replacement = listing_by_key.get((record['title'].casefold(), record['date']))
                if replacement is None:
                    candidates = [
                        item for item in listing_records
                        if item['date'] == record['date']
                        and record['title'].casefold().startswith(item['title'].casefold())
                    ]
                    replacement = candidates[0] if len(candidates) == 1 else None
                if replacement:
                    record['url'] = replacement['url']
                    record['time_from'] = replacement['time_from'] or record['time_from']
                    record['description'] = replacement['description'] or record['description']

            return sorted(
                records,
                key=lambda record: (record['date'], record['time_from'] or '', record['title']),
            )
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape Music Mountain events',
                event='crawler_scrape_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        finally:
            session.close()


def main():
    MusicmountainOrgCrawler().run()


if __name__ == '__main__':
    main()
