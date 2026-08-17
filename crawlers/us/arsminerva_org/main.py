import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.arsminerva.org/'
SOURCE = 'Ars Minerva'
CITY = 'San Francisco'
COUNTRY_CODE = 'US'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        start=1,
    )
}

OCCURRENCE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+'
    r'(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?\s*'
    r'@\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]m)',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def infer_year(month, day, weekday):
    """Resolve the omitted year using the printed weekday and nearby years."""
    today = date.today()
    candidates = []
    for year in range(today.year - 5, today.year + 3):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate.strftime('%A').lower() == weekday.lower():
            candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda value: abs((value - today).days)).year


def parse_occurrence(match):
    month = MONTHS[match.group('month').lower()]
    day = int(match.group('day'))
    year = int(match.group('year')) if match.group('year') else infer_year(
        month, day, match.group('weekday')
    )
    if year is None:
        return None
    try:
        event_date = date(year, month, day).isoformat()
    except ValueError:
        return None

    hour = int(match.group('hour')) % 12
    if match.group('ampm').lower() == 'pm':
        hour += 12
    return event_date, f'{hour:02d}:{int(match.group("minute")):02d}'


def make_record(title, event_date, time_from, venue, url, description):
    title = clean_text(title)
    venue = clean_text(venue)
    if not title or not event_date or not venue or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': COUNTRY_CODE,
        'description': clean_text(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_homepage(soup):
    text = clean_text(soup)
    links = {clean_text(link): urljoin(SOURCE_URL, link.get('href', ''))
             for link in soup.select('a[href]')}
    pattern = re.compile(
        r'(?P<title>Flower Piano 2026|Opera Day in the Bay)\s*/\s*'
        + OCCURRENCE_RE.pattern
        + r'\s*/\s*(?P<venue>[^\n]+)',
        re.IGNORECASE,
    )
    records = []
    for match in pattern.finditer(text):
        occurrence = parse_occurrence(match)
        if not occurrence:
            continue
        title = clean_text(match.group('title'))
        if title.lower().startswith('flower piano'):
            url = links.get('LEARN MORE…') or links.get('LEARN MORE...')
        else:
            url = links.get('RSVP')
        record = make_record(
            title, *occurrence, match.group('venue'), url,
            'Ars Minerva performance. ' + match.group(0),
        )
        if record:
            records.append(record)
    return records


def parse_production_page(soup, url):
    lines = [clean_text(line) for line in soup.get_text('\n').splitlines() if clean_text(line)]
    page_text = '\n'.join(lines)
    # The first h1 is the site's repeated masthead. Wix's document title
    # carries the actual production name on both production layouts.
    title = clean_text(soup.title).split('|')[0].split(' - ')[-1]

    matches = list(OCCURRENCE_RE.finditer(page_text))
    if not matches:
        return []

    first_line = next((index for index, line in enumerate(lines)
                       if OCCURRENCE_RE.search(line)), None)
    venue = None
    if first_line is not None:
        # Production pages put a venue name and street address directly before
        # the occurrence list. The address confirms the San Francisco default.
        preceding = lines[max(0, first_line - 3):first_line]
        if preceding and any('San Francisco' in line for line in preceding):
            address_index = next(
                index for index, line in enumerate(preceding) if 'San Francisco' in line
            )
            if address_index > 0:
                venue = preceding[address_index - 1]
    if not venue:
        return []

    description = page_text
    records = []
    for match in matches:
        occurrence = parse_occurrence(match)
        if occurrence:
            record = make_record(title, *occurrence, venue, url, description)
            if record:
                records.append(record)
    return records


class ArsminervaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='arsminerva_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SOURCE_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Ars Minerva homepage',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        home_soup = BeautifulSoup(response.text, 'html.parser')
        records = parse_homepage(home_soup)
        production_urls = {
            urljoin(SOURCE_URL, link.get('href', ''))
            for link in home_soup.select('a[href]')
            if urljoin(SOURCE_URL, link.get('href', '')) in {
                urljoin(SOURCE_URL, 'andromeda'),
                urljoin(SOURCE_URL, 'ercole-amante'),
            }
        }
        for url in sorted(production_urls):
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                records.extend(parse_production_page(
                    BeautifulSoup(detail_response.text, 'html.parser'), url
                ))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Ars Minerva production',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    ArsminervaOrgCrawler().run()


if __name__ == '__main__':
    main()
