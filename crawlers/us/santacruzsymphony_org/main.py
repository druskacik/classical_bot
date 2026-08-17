import re
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://santacruzsymphony.org/'
SOURCE = 'Santa Cruz Symphony'
OPEN_REHEARSALS_URL = urljoin(SOURCE_URL, 'open-rehearsals')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUES = {
    'civic auditorium': ('Santa Cruz Civic Auditorium', 'Santa Cruz'),
    'henry j. mello center': ('Henry J. Mello Center', 'Watsonville'),
    'mello center': ('Henry J. Mello Center', 'Watsonville'),
}

DATE_RE = re.compile(
    r'(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})'
    r'(?:,\s*(?P<year>20\d{2}))?'
    r'(?:\s+(?:at\s+)?|,\s*)'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
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
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def season_overview_url(soup):
    links = []
    for link in soup.select('a[href]'):
        href = urljoin(SOURCE_URL, link.get('href'))
        if re.search(r'/20\d{2}-\d{2}-concert-season-overview/?$', href):
            links.append(href)
    return sorted(set(links), reverse=True)[0] if links else None


def season_years(url):
    match = re.search(r'/(20\d{2})-(\d{2})-concert-season-overview', url)
    if not match:
        return None
    start = int(match.group(1))
    return start, start + 1


def parse_time(value):
    return datetime.strptime(value.upper().replace(' ', ''), '%I:%M%p').strftime('%H:%M') \
        if ':' in value else datetime.strptime(value.upper().replace(' ', ''), '%I%p').strftime('%H:%M')


def parse_occurrence(text, years):
    match = DATE_RE.search(text)
    if not match:
        return None
    month = datetime.strptime(match.group('month')[:3], '%b').month
    inferred_year = years[0] if month >= 7 else years[1]
    # Season context is more reliable than an occasional stale explicit year;
    # validate against the published weekday to catch those editorial typos.
    explicit_year = int(match.group('year')) if match.group('year') else inferred_year
    candidates = [explicit_year, inferred_year]
    event_date = None
    for year in dict.fromkeys(candidates):
        try:
            candidate = date(year, month, int(match.group('day')))
        except ValueError:
            continue
        if candidate.strftime('%A').lower() == match.group('weekday').lower():
            event_date = candidate
            break
    if event_date is None:
        return None
    return event_date.isoformat(), parse_time(match.group('time'))


def resolve_venue(text, default=None):
    lowered = text.lower()
    for label, location in VENUES.items():
        if label in lowered:
            return location
    return default


def section_nodes(main):
    nodes = main.find_all(['h1', 'h2', 'h3', 'p'], recursive=True)
    starts = []
    for index, node in enumerate(nodes):
        text = clean_text(node.get_text(' ', strip=True))
        if node.name == 'h1' and re.search(r'(Series Concert \d+|Family Concert|Pops Benefit Concert)', text, re.I):
            starts.append(index)
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(nodes)
        yield nodes[start:end]


def parse_season_page(soup, url):
    years = season_years(url)
    main = soup.select_one('main')
    if not years or not main:
        return []
    records = []
    for nodes in section_nodes(main):
        headings = [clean_text(node.get_text(' ', strip=True)) for node in nodes if node.name == 'h1']
        if len(headings) < 2:
            continue
        title = headings[1]
        all_text = [clean_text(node.get_text(' ', strip=True)) for node in nodes]
        description = clean_text('\n'.join(all_text)) or None
        mode = None
        for node in nodes:
            text = clean_text(node.get_text(' ', strip=True))
            if node.name in ('h2', 'h3'):
                if text.upper() == 'OPEN REHEARSAL':
                    mode = 'rehearsal'
                elif text.upper() in ('CONCERT DATE', 'CONCERT DATES'):
                    mode = 'concert'
                else:
                    mode = None
                continue
            if node.name != 'p' or mode not in ('concert', 'rehearsal'):
                continue
            occurrence = parse_occurrence(text, years)
            if not occurrence:
                continue
            default = VENUES['civic auditorium'] if mode == 'rehearsal' else None
            location = resolve_venue(text, default)
            if not location:
                continue
            record_title = f'{title} — Open Rehearsal' if mode == 'rehearsal' else title
            records.append({
                'title': record_title,
                'date': occurrence[0],
                'url': url,
                'time_from': occurrence[1],
                'venue': location[0],
                'city': location[1],
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def parse_archive_rehearsals(soup):
    main = soup.select_one('main')
    if not main:
        return []
    text = clean_text(main.get_text('\n', strip=True))
    season = re.search(r'OPEN REHEARSALS IN OUR (20\d{2})-(\d{2}) SEASON', text, re.I)
    if not season:
        return []
    years = (int(season.group(1)), int(season.group(1)) + 1)
    records = []
    archive = re.split(r'The work that goes into these rehearsals', text, maxsplit=1, flags=re.I)[0]
    archive = re.sub(r'\s+', ' ', archive)
    for match in DATE_RE.finditer(archive):
        occurrence = parse_occurrence(match.group(0), years)
        title_match = re.match(r'\s*\(([^()]+)\)', archive[match.end():])
        if not occurrence or not title_match:
            continue
        records.append({
            'title': f'{clean_text(title_match.group(1))} — Open Rehearsal',
            'date': occurrence[0],
            'url': OPEN_REHEARSALS_URL,
            'time_from': occurrence[1],
            'venue': 'Santa Cruz Civic Auditorium',
            'city': 'Santa Cruz',
            'country_code': 'US',
            'description': text,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    homepage = get_soup(session, SOURCE_URL)
    overview_url = season_overview_url(homepage)
    if not overview_url:
        log_message(
            'No concert season overview link found',
            event='crawler_source_missing',
            level='warning',
            url=SOURCE_URL,
        )
        return []

    records = parse_season_page(get_soup(session, overview_url), overview_url)
    try:
        records.extend(parse_archive_rehearsals(get_soup(session, OPEN_REHEARSALS_URL)))
    except requests.RequestException as error:
        log_message(
            'Failed to scrape open rehearsal archive',
            event='crawler_item_failed',
            level='warning',
            url=OPEN_REHEARSALS_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    unique = {(record['title'], record['date'], record['time_from'], record['venue']): record for record in records}
    return sorted(unique.values(), key=lambda record: (record['date'], record['time_from'], record['title']))


class SantaCruzSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='santacruzsymphony_org',
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
        return get_concerts()


def main():
    SantaCruzSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
