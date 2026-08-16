import re
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nisorchestra.org/'
SEASON_URL = urljoin(SOURCE_URL, 'subscriptions')
SOURCE = 'Northwest Indiana Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    name: number
    for number, name in enumerate(
        (
            'January', 'February', 'March', 'April', 'May', 'June',
            'July', 'August', 'September', 'October', 'November', 'December',
        ),
        start=1,
    )
}

DATE_RE = re.compile(
    r'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:,\s*\d{4})?(?:,?\s+(\d{1,2}):(\d{2})\s*([ap]m))?$',
    re.I,
)

VENUE_CITIES = {
    'Sola Center at Illiana Christian High School': 'Dyer',
    'Hard Rock Casino': 'Gary',
    'Hard Rock Live': 'Gary',
    'Valparaiso High School': 'Valparaiso',
    'Living Hope Church': 'Merrillville',
    'Theatre at the Center': 'Munster',
}


def clean_text(value):
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_anchor(html):
    text = clean_text(BeautifulSoup(html, 'html.parser').select_one('main'))
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{1,2},\s*(20\d{2})',
        text,
        re.I,
    )
    if not match:
        raise ValueError('Could not determine the season year from the homepage')
    return int(match.group(2)), MONTHS[match.group(1).title()]


def event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return []
    urls = []
    for link in main.select('a[href]'):
        url = urljoin(SEASON_URL, link['href']).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path not in ('/', '/subscriptions'):
            if url not in urls:
                urls.append(url)
    return urls


def parse_time(hour, minute, meridiem):
    return datetime.strptime(f'{hour}:{minute}{meridiem}', '%I:%M%p').strftime('%H:%M')


def parse_venue(value):
    venue = re.sub(r'\s*\([^)]*(?:age\s*)?21[^)]*\)\s*', '', value, flags=re.I).strip()
    venue = re.sub(r',\s*(?:Munster|Dyer|Gary|Merrillville|Valparaiso)\s*$', '', venue).strip()
    if venue == 'Hard Rock Casino':
        return venue, 'Gary'
    return venue, VENUE_CITIES.get(venue)


def parse_event(html, url, anchor_year, anchor_month):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    heading = main.select_one('h1') if main else None
    title = clean_text(heading).replace('\n', ' ') if heading else ''
    text = clean_text(main)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not title or not lines:
        return []

    records = []
    for index, line in enumerate(lines):
        match = DATE_RE.match(line)
        if not match:
            continue
        month = MONTHS[match.group(1).title()]
        year = anchor_year if month >= anchor_month else anchor_year + 1
        try:
            event_date = date(year, month, int(match.group(2))).isoformat()
        except ValueError:
            continue
        time_parts = match.group(3), match.group(4), match.group(5)
        if not all(time_parts):
            concert_time = next(
                (
                    re.search(r'^Concert\s*[-–:]\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)$', candidate, re.I)
                    for candidate in lines[index + 1:index + 5]
                    if re.search(r'^Concert\s*[-–:]', candidate, re.I)
                ),
                None,
            )
            if not concert_time:
                continue
            time_parts = (
                concert_time.group(1), concert_time.group(2) or '00', concert_time.group(3)
            )

        venue = city = None
        for candidate in lines[index + 1:index + 6]:
            candidate_venue, candidate_city = parse_venue(candidate)
            if candidate_city:
                venue, city = candidate_venue, candidate_city
                break
        if not venue or not city:
            log_message(
                'Skipped NWI Symphony occurrence with an unknown venue or city',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Venue or city could not be defensibly inferred',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(*time_parts),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class NisorchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nisorchestra_org',
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

        home_response = session.get(SOURCE_URL, timeout=45)
        home_response.raise_for_status()
        anchor_year, anchor_month = season_anchor(home_response.text)

        season_response = session.get(SEASON_URL, timeout=45)
        season_response.raise_for_status()
        urls = event_urls(season_response.text)

        records = []
        for url in urls:
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                records.extend(parse_event(response.text, url, anchor_year, anchor_month))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape NWI Symphony concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'], item['title'], item['venue']),
        )


def main():
    NisorchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
