import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://berkshirebach.org/'
SOURCE = 'Berkshire Bach Society'
PAGES = (
    f'{SOURCE_URL}events',
    f'{SOURCE_URL}events3',
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|'
    r'JUL(?:Y)?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|'
    r'NOV(?:EMBER)?|DEC(?:EMBER)?)\s*[- ]?\s*(\d{1,2})'
    r'(?:\s*,?\s*(20\d{2}))?',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?(?=\s|$)', re.I)
CITY_STATE_RE = re.compile(
    r'\b([A-Za-z][A-Za-z .\-/]+?),\s*(MA|NY|VT|CT)\b', re.I
)
SEASON_RE = re.compile(r'\b(20\d{2})\s*[-–]\s*(20\d{2})\s+Season\b', re.I)


def clean_text(value):
    value = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem}M', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def date_entries(value, season_years):
    matches = list(DATE_RE.finditer(value or ''))
    entries = []
    for index, match in enumerate(matches):
        month, day, explicit_year = match.groups()
        month_number = datetime.strptime(month[:3].title(), '%b').month
        year = int(explicit_year) if explicit_year else (
            season_years[0] if month_number >= 7 else season_years[1]
        )
        try:
            event_date = datetime(year, month_number, int(day)).date().isoformat()
        except ValueError:
            continue
        tail_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        time_from = parse_time(value[match.end():tail_end])
        item = (event_date, time_from)
        if item not in entries:
            entries.append(item)
    return entries


def is_date_block(text):
    without_dates = DATE_RE.sub(' ', text)
    without_times = TIME_RE.sub(' ', without_dates)
    without_punctuation = re.sub(r'[\s,&\-/|]+', '', without_times)
    return bool(DATE_RE.search(text)) and len(without_punctuation) < 18


def title_from_detail(detail):
    for node in detail.select('h1, h2, h3, h4, strong'):
        title = clean_text(node.get_text(' ', strip=True))
        if title and not DATE_RE.fullmatch(title):
            return title
    lines = [line for line in clean_text(detail.get_text('\n', strip=True)).splitlines() if line]
    for line in lines:
        if not DATE_RE.search(line) and not SEASON_RE.search(line):
            return line
    return ''


def location_for_entry(detail_text, event_date):
    lines = [line.strip(' |') for line in detail_text.splitlines() if line.strip(' |')]
    date_obj = datetime.strptime(event_date, '%Y-%m-%d')
    month_name = date_obj.strftime('%B')
    day = str(date_obj.day)

    # Touring programmes list a date followed by that occurrence's hall and city.
    start = 0
    for index, line in enumerate(lines):
        if re.search(rf'\b{month_name}\s+0?{day}\b', line, re.I):
            start = index
            break
    search_lines = lines[start:start + 8] if start else lines[:12]
    searchable = '\n'.join(search_lines).upper()
    known_locations = (
        ('BERKSHIRE WALDORF HIGH SCHOOL', 'Berkshire Waldorf High School', 'Stockbridge'),
        ('STUDIO E, THE LINDE CENTER', 'Studio E, Linde Center for Music and Learning', 'Lenox'),
        ('STUDIO E, LINDE CENTER', 'Studio E, Linde Center for Music and Learning', 'Lenox'),
        ('SAINT JAMES PLACE', 'Saint James Place', 'Great Barrington'),
        ('ST. PAUL’S EPISCOPAL CHURCH', "St. Paul's Episcopal Church", 'Stockbridge'),
        ('UNITARIAN UNIVERSALIST MEETING', 'Unitarian Universalist Meeting of South Berkshire', 'Housatonic'),
        ('FIRST CONGREGATIONAL CHURCH', 'First Congregational Church', 'Great Barrington'),
        ('LENOX TOWN HALL', 'Lenox Town Hall', 'Lenox'),
        ('NEW MARLBOROUGH MEETING HOUSE', 'New Marlborough Meeting House', 'New Marlborough'),
    )
    for marker, venue, city in known_locations:
        if marker in searchable:
            return venue, city

    whole_text = detail_text.upper()
    if 'PETER SYKES: MUSIC FOR HARPSICHORD AND CLAVICHORD' in whole_text:
        return 'Berkshire Waldorf High School', 'Stockbridge'
    if 'LOUPRETTE PLAYS THE HISTORIC JOHNSON ORGAN' in whole_text:
        return 'Unitarian Universalist Meeting of South Berkshire', 'Housatonic'
    if 'SYKES PLAYS THE GREAT ROOSEVELT ORGAN' in whole_text:
        return 'First Congregational Church', 'Great Barrington'
    for index, line in enumerate(search_lines):
        match = CITY_STATE_RE.search(line)
        if not match:
            continue
        raw_city = match.group(1).strip(' |')
        city = re.split(r'\s*/\s*|\s*\|\s*', raw_city)[-1].strip()
        prefix = line[:match.start()].strip(' |')
        venue = prefix or (search_lines[index - 1].strip(' |') if index else '')
        venue = venue.rstrip(' (')
        if re.match(r'^\d+\s', venue) and index:
            venue = search_lines[index - 1].strip(' |')
        if venue.upper() == 'HIGH SCHOOL' and city == 'Stockbridge':
            venue = 'Berkshire Waldorf High School'
        venue = re.sub(r'^Co-presented with .+$', '', venue, flags=re.I).strip()
        if venue and city and venue.lower() != city.lower():
            return venue, city

    return '', ''


def scrape_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    blocks = soup.select('main .sqs-block-html')
    records = []
    season_years = None
    paired_detail_indexes = set()

    for index, block in enumerate(blocks):
        text = clean_text(block.get_text('\n', strip=True))
        season_match = SEASON_RE.search(text)
        if season_match:
            season_years = tuple(map(int, season_match.groups()))
        if not season_years or not is_date_block(text) or index + 1 >= len(blocks):
            continue

        detail = blocks[index + 1]
        paired_detail_indexes.add(index + 1)
        detail_text = clean_text(detail.get_text('\n', strip=True))
        if is_date_block(detail_text):
            continue
        title = title_from_detail(detail)
        if not title or len(detail_text) < 20:
            continue

        entries = date_entries(text, season_years)
        nearby_date_blocks = sum(
            is_date_block(clean_text(item.get_text('\n', strip=True)))
            for item in blocks[index + 2:index + 6]
        )
        if len(entries) > 1 and nearby_date_blocks >= len(entries):
            continue
        # Multi-city series often repeat exact dates, times, and locations in prose.
        prose_entries = date_entries(detail_text, season_years)
        if len(entries) > 1 and len(prose_entries) >= len(entries):
            entries = prose_entries[:len(entries)]

        for event_date, time_from in entries:
            venue, city = location_for_entry(detail_text, event_date)
            if not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': f'{url}#{detail.get("id", "")}',
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': detail_text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    # Older archive seasons use one rich-text block per event instead of the
    # newer alternating date/detail layout. Parse those blocks when they carry
    # an explicit year and a defensible venue/city pair.
    embedded_season = None
    for index, block in enumerate(blocks):
        block_text = clean_text(block.get_text('\n', strip=True))
        season_match = SEASON_RE.search(block_text)
        if season_match:
            embedded_season = tuple(map(int, season_match.groups()))
        if index in paired_detail_indexes:
            continue
        detail_text = block_text
        if is_date_block(detail_text) or not re.search(r'\b20\d{2}\b', detail_text):
            continue
        title = title_from_detail(block)
        if not title or SEASON_RE.search(title):
            continue
        if not embedded_season:
            continue
        for event_date, time_from in date_entries(detail_text, embedded_season):
            venue, city = location_for_entry(detail_text, event_date)
            if not venue or not city:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': f'{url}#{block.get("id", "")}',
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': 'US',
                'description': detail_text,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class BerkshireBachOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='berkshirebach_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url in PAGES:
            try:
                records.extend(scrape_page(session, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Berkshire Bach events page',
                    event='crawler_page_failed',
                    level='error',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        if not records:
            log_message(
                'No valid Berkshire Bach events found',
                event='crawler_empty_listing',
                level='warning',
                url=PAGES[0],
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['venue']))


def main():
    BerkshireBachOrgCrawler().run()


if __name__ == '__main__':
    main()
