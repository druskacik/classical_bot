import re
from datetime import datetime
import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chestermusicsociety.org.uk/'
SOURCE = 'Chester Music Society'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
CALENDAR_SLUG = 'concert-calendar'
DETAIL_SLUGS = ('concerts-at-st-marys', 'choir-concerts')
COMPETITION_SLUG = 'chester-young-musician'
CITY = 'Chester'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}
DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?\s*'
    r'(\d{1,2})(?:st|nd|rd|th)?\s+'
    r'(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    r'\s+(20\d{2})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(value or '')
    if not match:
        return None
    try:
        day, month, year = match.groups()
        month = 'Sep' if month.lower() == 'sept' else month
        return datetime.strptime(f'{day} {month[:3]} {year}', '%d %b %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = TIME_RE.search(value or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def fetch_page(session, slug):
    response = session.get(
        API_URL,
        params={'slug': slug, '_fields': 'link,title,content'},
        timeout=45,
    )
    response.raise_for_status()
    pages = response.json()
    if not pages:
        raise ValueError(f'WordPress page not found for slug {slug!r}')
    return pages[0]


def table_fields(nodes):
    fields = {}
    for node in nodes:
        for row in node.select('tr'):
            cells = row.find_all(['th', 'td'], recursive=False)
            if len(cells) != 2:
                continue
            label = clean_text(cells[0]).rstrip(':').lower()
            if label in {'date', 'time', 'venue'}:
                fields[label] = clean_text(cells[1])
    return fields


def section_description(nodes):
    parts = []
    for node in nodes:
        clone = BeautifulSoup(str(node), 'html.parser')
        for row in clone.select('tr'):
            cells = row.find_all(['th', 'td'], recursive=False)
            if cells and clean_text(cells[0]).rstrip(':').lower() in {
                'date', 'time', 'venue', 'tickets', 'ticket',
            }:
                row.decompose()
        for link in clone.select('a'):
            if re.search(r'\b(?:buy|book)\s+tickets?\b', clean_text(link), re.IGNORECASE):
                link.decompose()
        text = clean_text(clone)
        if text and text not in {'\u00b7\u00b7\u00b7', 'or pay at the door.', 'or pay on the door'}:
            parts.append(text)
    description = '\n\n'.join(parts).strip()
    return description or None


def detail_sections(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    sections = {}
    for heading in soup.select('h2'):
        nodes = []
        for sibling in heading.next_siblings:
            if getattr(sibling, 'name', None) == 'h2':
                break
            if getattr(sibling, 'name', None):
                nodes.append(sibling)
        fields = table_fields(nodes)
        event_date = parse_date(fields.get('date'))
        if not event_date or not fields.get('venue'):
            continue
        sections[event_date] = {
            'title': clean_text(heading),
            'time_from': parse_time(fields.get('time')),
            'venue': fields['venue'],
            'description': section_description(nodes),
        }
    return sections


def calendar_events(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    events = []
    for row in soup.select('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) != 2:
            continue
        event_date = parse_date(clean_text(cells[0]))
        link = cells[1].find('a', href=True)
        title = clean_text(link or cells[1])
        if event_date and link and title:
            events.append({'title': title, 'date': event_date, 'url': link['href']})
    return events


def competition_event(page):
    soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
    text = clean_text(soup)
    when_where = re.search(
        r'When and Where\s+(.*?)(?=\nCompetition Format\b)', text, re.IGNORECASE | re.DOTALL
    )
    if not when_where:
        return None
    event_date = parse_date(when_where.group(1))
    if not event_date or 'open to the public' not in text.lower():
        return None
    return {
        'title': clean_text(BeautifulSoup(page['title']['rendered'], 'html.parser')),
        'date': event_date,
        'url': page['link'],
        'time_from': None,
        'venue': 'Abbey Gate College, Saighton Grange',
        'city': CITY,
        'description': text,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    calendar = fetch_page(session, CALENDAR_SLUG)
    details = {}
    for slug in DETAIL_SLUGS:
        details.update(detail_sections(fetch_page(session, slug)))

    records = []
    for item in calendar_events(calendar):
        detail = details.get(item['date'])
        if not detail:
            log_message(
                'Skipping calendar concert without complete detail data',
                event='crawler_item_skipped',
                level='warning',
                url=item['url'],
            )
            continue
        records.append(
            {
                'title': detail['title'] or item['title'],
                'date': item['date'],
                'url': item['url'],
                'time_from': detail['time_from'],
                'venue': detail['venue'],
                'city': CITY,
                'description': detail['description'],
            }
        )

    competition = competition_event(fetch_page(session, COMPETITION_SLUG))
    if competition:
        records.append(competition)

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class ChesterMusicSocietyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chestermusicsociety_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    ChesterMusicSocietyCrawler().run()


if __name__ == '__main__':
    main()
