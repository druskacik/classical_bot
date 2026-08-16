import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jhchambermusic.org/'
SOURCE = 'Jackson Hole Chamber Music'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

DATE_RE = re.compile(
    r'(?i)^(?:(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|'
    r'thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\s*,?\s+)?'
    r'(january|february|march|april|may|june|july|august|september|october|'
    r'november|december)\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(20\d{2})'
    r'(?:\s*(?:,|\|)\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?)?$'
)
TIME_RE = re.compile(r'(?i)^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?$')

VENUES = (
    ('rustic pine tavern', 'Rustic Pine Tavern', 'Dubois'),
    ('dennison lodge', 'Dennison Lodge', 'Dubois'),
    ('antelope trails ranch', 'Antelope Trails Ranch', 'Jackson'),
    ('center for the arts', 'Center for the Arts', 'Jackson'),
    ('jackson hole center for the arts', 'Center for the Arts', 'Jackson'),
    ('private residence of beth and ben wegbreit',
     'Private Residence of Beth and Ben Wegbreit', 'Jackson'),
)


def clean_lines(html):
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup.select('script, style, svg, .elementor-button-wrapper'):
        element.decompose()
    text = soup.get_text('\n', strip=True).replace('\xa0', ' ')
    lines = []
    for line in text.splitlines():
        line = re.sub(r'\s+', ' ', line).strip(' \t–|')
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return lines


def parse_date_line(line):
    match = DATE_RE.fullmatch(line.strip())
    if not match:
        return None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None
    time_from = normalize_time(match.group(4), match.group(5), match.group(6))
    return event_date, time_from


def normalize_time(hour, minute, meridiem):
    if not hour or not meridiem:
        return None
    hour = int(hour)
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def find_location(lines):
    combined = ' '.join(lines).lower()
    for needle, venue, city in VENUES:
        if needle in combined:
            return venue, city
    return None


def find_title(lines):
    ignored = re.compile(
        r'(?i)^(?:at |tickets?|purchase|buy |click |save the date|program(?:me)?\s*:|'
        r'repertoire\s*:|doors? open|featuring works|hosted |a fundraiser|'
        r'\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)'
    )
    for line in lines:
        if ignored.search(line) or find_location([line]):
            continue
        title = re.sub(r'(?i)^concert\s*\d+\s*:\s*', '', line).strip()
        if len(title) >= 3 and not re.fullmatch(r'[&+\s]+', title):
            return title
    return None


def parse_page(page):
    lines = clean_lines(page.get('content', {}).get('rendered', ''))
    date_positions = []
    for index, line in enumerate(lines):
        parsed = parse_date_line(line)
        if parsed:
            date_positions.append((index, parsed))

    records = []
    for position, (start, (event_date, time_from)) in enumerate(date_positions):
        end = date_positions[position + 1][0] if position + 1 < len(date_positions) else len(lines)
        # Older festival pages append long musician biographies after the final
        # programme. Keep the event block bounded so venues mentioned in a bio
        # cannot be mistaken for the concert venue.
        details = lines[start + 1:min(end, start + 60)]
        location = find_location(details)
        title = find_title(details)
        if not location or not title:
            continue

        if time_from is None:
            for line in details:
                match = TIME_RE.fullmatch(line)
                if match:
                    time_from = normalize_time(*match.groups())
                    break

        venue, city = location
        description = '\n'.join(details).strip() or None
        records.append({
            'title': title,
            'date': event_date,
            'url': page['link'],
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
        })
    return records


def is_concert_page(page):
    slug = page.get('slug', '').lower()
    title = BeautifulSoup(page.get('title', {}).get('rendered', ''), 'html.parser').get_text()
    if any(word in slug for word in ('musician', 'artwork', 'gallery')):
        return False
    return 'festival' in slug or 'concert' in slug or 'festival' in title.lower()


class JhChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jhchambermusic_org',
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
                params={'per_page': 100, 'page': 1},
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            pages = response.json()
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to fetch Jackson Hole Chamber Music pages',
                event='crawler_fetch_failed',
                level='error',
                url=API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for page in pages:
            if is_concert_page(page):
                records.extend(parse_page(page))
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    JhChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
