import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fwopera.org/'
PERFORMANCES_URL = urljoin(SOURCE_URL, 'performances')
SOURCE = 'Fort Worth Opera'
CITY = 'Fort Worth'

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
        1,
    )
}
DATE_RE = re.compile(
    r'(?P<month>' + '|'.join(MONTHS) + r')\s+'
    r'(?P<start>\d{1,2})(?:\s*[–-]\s*(?P<end>\d{1,2}))?'
    r'(?:,?\s+(?P<year>20\d{2}))?',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2}):([0-5]\d)\s*([ap])\.?m\.?', re.I)
VENUE_RE = re.compile(
    r'\b(?:Theater|Theatre|Hall|Museum|Pavilion|Zoo|Club|Center|Centre|Auditorium)\b',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def performance_cards(soup):
    cards = []
    seen = set()
    for container in soup.select(
        'div[data-mesh-id*="__item-"][data-mesh-id$="inlineContent-gridContainer"]'
    ):
        link = container.select_one('a[aria-label="Learn More"][href]')
        if not link:
            continue
        url = urljoin(SOURCE_URL, link.get('href'))
        parsed = urlparse(url)
        if parsed.netloc != urlparse(SOURCE_URL).netloc or url in seen:
            continue
        lines = [clean_text(node) for node in container.stripped_strings]
        lines = [line for line in lines if line]
        title = next(
            (line for line in lines if line not in ('Learn More', 'Buy Tickets')),
            '',
        )
        if title and DATE_RE.search('\n'.join(lines)):
            cards.append({'title': title, 'url': url, 'lines': lines})
            seen.add(url)
    return cards


def infer_year(month, explicit_years):
    if explicit_years:
        low, high = min(explicit_years), max(explicit_years)
        return low if month >= 7 else high
    today = date.today()
    return today.year if month >= today.month else today.year + 1


def parse_time(value):
    match = TIME_RE.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def venue_from_segment(segment):
    for line in segment:
        for part in re.split(r'\s*\|\s*', line):
            value = clean_text(part).strip(' |,–-')
            if value and len(value) <= 90 and VENUE_RE.search(value):
                return re.split(r'\s*[–-]\s*', value, maxsplit=1)[0].strip(' ,')
        value = clean_text(line).strip(' |,–-')
        if (
            value
            and len(value) <= 90
            and VENUE_RE.search(value)
            and not DATE_RE.search(value)
            and not value.lower().startswith(('buy ', 'learn '))
        ):
            return re.split(r'\s*[–-]\s*', value, maxsplit=1)[0].strip(' ,')
    return None


def detail_venue(lines):
    text = ' '.join(lines)
    woman_club = re.search(r"The Woman[’']s Club of Fort Worth", text, re.I)
    if woman_club:
        return clean_text(woman_club.group(0))
    candidates = []
    for line in lines:
        value = clean_text(line).strip(' |,–-')
        if not value or len(value) > 90 or not VENUE_RE.search(value):
            continue
        if re.search(r'\b(?:at|to|of)\s+(?:the\s+)?' + re.escape(value) + r'\b', value, re.I):
            continue
        if not re.search(r'[.!?]', value):
            candidates.append(re.sub(r',\s*\d+\s+.*$', '', value).strip())
    if candidates:
        return candidates[0]

    # Some pages introduce the location only in their opening prose.
    match = re.search(
        r'\bat\s+(?P<venue>(?:The\s+)?[A-Z][^,.]{2,70}'
        r'(?:Theater|Theatre|Hall|Museum|Pavilion|Zoo|Club|Center|Centre|Auditorium))',
        text,
    )
    return clean_text(match.group('venue')) if match else None


def detail_times(event_date, detail_lines, explicit_years):
    target = date.fromisoformat(event_date)
    times = []
    for line in detail_lines:
        for match in DATE_RE.finditer(line):
            month = MONTHS[match.group('month').title()]
            year = (
                int(match.group('year'))
                if match.group('year')
                else infer_year(month, explicit_years)
            )
            end_day = int(match.group('end') or match.group('start'))
            if year != target.year or month != target.month:
                continue
            if not int(match.group('start')) <= target.day <= end_day:
                continue
            for time_match in TIME_RE.finditer(line[match.start():]):
                parsed = parse_time(time_match.group(0))
                if parsed and parsed not in times:
                    times.append(parsed)
    return times


def occurrences(card, detail_lines):
    lines = card['lines']
    card_matches = list(DATE_RE.finditer('\n'.join(lines)))
    explicit_years = [int(match.group('year')) for match in card_matches if match.group('year')]
    explicit_months = [
        MONTHS[match.group('month').title()]
        for match in card_matches
        if match.group('year')
    ]
    detail_years = [
        int(match.group('year'))
        for match in DATE_RE.finditer('\n'.join(detail_lines))
        if match.group('year')
    ]
    all_years = explicit_years + detail_years
    fallback_venue = detail_venue(detail_lines)
    results = []
    for index, line in enumerate(lines):
        matches = list(DATE_RE.finditer(line))
        for match in matches:
            month = MONTHS[match.group('month').title()]
            if match.group('year'):
                year = int(match.group('year'))
            elif explicit_years and month >= 7 and explicit_months and max(explicit_months) < 7:
                year = min(explicit_years) - 1
            else:
                year = infer_year(month, explicit_years)
            start_day = int(match.group('start'))
            end_day = int(match.group('end') or start_day)
            segment = lines[index:index + 4]
            time_from = parse_time(' '.join(segment))
            venue = venue_from_segment(segment) or fallback_venue
            for day in range(start_day, end_day + 1):
                try:
                    event_date = date(year, month, day).isoformat()
                except ValueError:
                    continue
                times = detail_times(event_date, detail_lines, all_years)
                for resolved_time in times or [time_from]:
                    results.append((event_date, resolved_time, venue))
    return results


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    listing = get_soup(session, PERFORMANCES_URL)
    records = []
    for card in performance_cards(listing):
        try:
            detail = get_soup(session, card['url'])
        except requests.RequestException as error:
            log_message(
                'Failed to scrape concert detail',
                event='crawler_item_failed',
                level='warning',
                url=card['url'],
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        main = detail.select_one('main')
        if not main:
            continue
        detail_lines = [clean_text(value) for value in main.stripped_strings]
        detail_lines = [value for value in detail_lines if value]
        description = clean_text(main.get_text('\n', strip=True)) or None
        for event_date, time_from, venue in occurrences(card, detail_lines):
            if not venue:
                log_message(
                    'Skipping performance without a defensible venue',
                    event='crawler_item_skipped',
                    level='warning',
                    url=card['url'],
                    date=event_date,
                )
                continue
            records.append({
                'title': card['title'],
                'date': event_date,
                'url': card['url'],
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    unique = {
        (record['url'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class FwoperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fwopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    FwoperaOrgCrawler().run()


if __name__ == '__main__':
    main()
