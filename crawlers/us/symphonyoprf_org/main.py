import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://symphonyoprf.org/'
SOURCE = 'The Symphony of Oak Park & River Forest'
API_URL = 'https://public-api.wordpress.com/rest/v1.1/sites/symphonyoprf.org/posts/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_PATTERN = re.compile(
    r'(?P<date>(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})',
    re.IGNORECASE,
)
TIME_PATTERN = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([AP])\.?M\.?', re.I)
SEASON_SLUG = re.compile(r'^(?:our-)?20\d{2}-20\d{2}-season$')


def clean_text(value):
    value = value.replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def parse_date(value):
    match = DATE_PATTERN.search(value)
    if not match:
        return None
    raw = match.group('date')
    for date_format in ('%B %d, %Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(raw.title(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = TIME_PATTERN.search(value)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).upper() == 'P':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def is_event_marker(line):
    if not DATE_PATTERN.search(line):
        return False
    if re.search(r'program|tickets?|purchase|concert book', line, re.I):
        return False
    return bool(
        TIME_PATTERN.search(line)
        or re.match(
            r'^(?:January|February|March|April|May|June|July|August|September|'
            r'October|November|December)\s+\d{1,2},\s+20\d{2}\b',
            line,
            re.IGNORECASE,
        )
    )


def title_for_marker(lines, index):
    line = lines[index]
    date_match = DATE_PATTERN.search(line)
    suffix = line[date_match.end():].strip(' –—-:') if date_match else ''
    suffix = re.sub(r'^\d{1,2}:\d{2}\s*[AP]M\s*', '', suffix, flags=re.I).strip(' –—-:')
    if suffix and not re.fullmatch(r'(?:SYMPHONY CENTER|Concordia University(?: Chapel)?)', suffix, re.I):
        return suffix

    following_parts = []
    candidates = lines[index + 1:index + 5]
    while candidates and re.fullmatch(r'[\W_]+', candidates[0]):
        candidates.pop(0)
    if candidates and re.search(r'program|download', candidates[0], re.I):
        candidates = []
    for candidate in candidates:
        if re.search(r'Concordia|conductor|tickets?', candidate, re.I):
            break
        if re.search(r'program|download', candidate, re.I):
            continue
        if re.fullmatch(r'[\W_]+', candidate):
            continue
        following_parts.append(candidate)
        if len(' '.join(following_parts)) >= 12:
            break
    if following_parts and not re.search(r'Join us|program includes|receive all our', following_parts[0], re.I):
        return ''.join(following_parts).strip(' /–—-:')

    previous = []
    for candidate in reversed(lines[max(0, index - 2):index]):
        if not re.fullmatch(r'[\W_]+|(?:th|st|nd|rd)', candidate, re.I):
            previous.insert(0, candidate)
    title = ' '.join(previous).strip(' –—-:')
    if re.search(r'receive all our|sign up|purchase|tickets?', title, re.I):
        title = ''
    return title or 'Symphony of Oak Park & River Forest Concert'


def location_for_chunk(marker, description):
    combined = f'{marker}\n' + '\n'.join(description.splitlines()[:15])
    if re.search(r'Symphony Center', marker, re.I) or re.search(
        r'(?:concert|live)[^\n.]{0,80}Symphony Center', combined, re.I
    ):
        return 'Symphony Center', 'Chicago'
    if re.search(r'Concordia University Chapel', combined, re.I):
        return 'Concordia University Chapel', 'River Forest'
    if re.search(r'Concordia University', combined, re.I):
        return 'Concordia University', 'River Forest'
    return 'Concordia University', 'River Forest'


def parse_season_page(page):
    soup = BeautifulSoup(page.get('content', ''), 'html.parser')
    lines = [clean_text(line) for line in soup.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    markers = [index for index, line in enumerate(lines) if is_event_marker(line)]
    records = []

    for position, index in enumerate(markers):
        end = markers[position + 1] if position + 1 < len(markers) else len(lines)
        marker = lines[index]
        event_date = parse_date(marker)
        title = title_for_marker(lines, index)
        description = clean_text('\n'.join(lines[index:end])) or None
        immediate = lines[index + 1:end]
        if immediate and (
            re.fullmatch(r'[\W_]+', immediate[0])
            or re.search(r'program|download', immediate[0], re.I)
        ):
            description = clean_text(f'{marker}\n{title}')
        venue, city = location_for_chunk(marker, description or '')
        if not event_date or not title:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': page['URL'].replace('http://', 'https://', 1),
            'time_from': parse_time(marker) or '16:00',
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class SymphonyoprfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='symphonyoprf_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(
                API_URL,
                params={'type': 'page', 'number': 100},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Symphony OPRF pages',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        pages = [page for page in payload.get('posts', []) if SEASON_SLUG.fullmatch(page['slug'])]
        records = []
        for page in pages:
            records.extend(parse_season_page(page))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SymphonyoprfOrgCrawler().run()


if __name__ == '__main__':
    main()
