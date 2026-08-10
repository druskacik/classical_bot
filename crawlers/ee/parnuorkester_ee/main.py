import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://parnuorkester.ee/'
SOURCE = 'Pärnu Linnaorkester'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'

# Estonian-language event categories. English posts duplicate the same events.
EVENT_CATEGORIES = (331, 329)  # Tulekul, Toimunud

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'et-EE,et;q=0.9,en;q=0.7',
}

DATE_TIME_RE = re.compile(
    r'(?P<day>\d{1,2})\s*\.\s*(?P<month>\d{1,2})\s*\.\s*'
    r'(?P<year>20\d{2})\s*(?:kell|kl\.?)[\s:]*'
    r'(?P<hour>[01]?\d|2[0-3])(?:[.:](?P<minute>[0-5]\d))?',
    re.IGNORECASE,
)

MONTH_DATE_TIME_RE = re.compile(
    r'(?P<day>\d{1,2})\s*\.?(?:\s+)'
    r'(?P<month_name>jaanuar|veebruar|märts|aprill|mai|juuni|juuli|'
    r'august|september|oktoober|november|detsember)(?:il)?\s+'
    r'(?P<year>20\d{2})\s*(?:kell|kl\.?)[\s:]*'
    r'(?P<hour>[01]?\d|2[0-3])(?:[.:](?P<minute>[0-5]\d))?',
    re.IGNORECASE,
)

MONTHS = {
    'jaanuar': 1, 'veebruar': 2, 'märts': 3, 'aprill': 4,
    'mai': 5, 'juuni': 6, 'juuli': 7, 'august': 8,
    'september': 9, 'oktoober': 10, 'november': 11, 'detsember': 12,
}

# Match longer/more specific aliases first. Each name is evidence for both the
# venue and city; no orchestra-home fallback is applied to touring events.
VENUES = (
    (re.compile(r'KUMU\s+auditoorium(?:is)?', re.IGNORECASE), 'Kumu auditoorium', 'Tallinn'),
    (re.compile(r'Pärnu\s+kontserdimaja(?:s)?', re.IGNORECASE), 'Pärnu kontserdimaja', 'Pärnu'),
    (re.compile(r'(?:Heino Elleri Muusikakool,?\s*)?Tubina saal(?:is)?', re.IGNORECASE), 'Tubina saal', 'Tartu'),
    (re.compile(r'Estonia\s+kontserdisaal(?:is)?', re.IGNORECASE), 'Estonia kontserdisaal', 'Tallinn'),
    (re.compile(r'Jõhvi\s+kontserdimaja(?:s)?', re.IGNORECASE), 'Jõhvi kontserdimaja', 'Jõhvi'),
    (re.compile(r'Vanemuise\s+kontserdimaja(?:s)?', re.IGNORECASE), 'Vanemuise kontserdimaja', 'Tartu'),
)

RELATED_MARKERS = (
    'TULEKUL KONTSERDID',
    'TOIMUNUD KONTSERDID',
    'VAATA KÕIKI TULEKUL KONTSERTE',
)


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def event_text(rendered_html):
    soup = BeautifulSoup(rendered_html or '', 'html.parser')
    for element in soup.select('script, style, noscript, form'):
        element.decompose()
    text = clean_text(soup.get_text('\n', strip=True))
    marker_positions = [
        position
        for marker in RELATED_MARKERS
        if (position := text.upper().find(marker)) >= 0
    ]
    if marker_positions:
        text = text[:min(marker_positions)]
    return clean_text(text)


def venue_after(text, match):
    # Venue text follows the time on this site. A bounded window prevents a
    # later biography or related-event widget from being mistaken for it.
    context = text[match.end():match.end() + 180]
    candidates = []
    for pattern, venue, city in VENUES:
        venue_match = pattern.search(context)
        if venue_match:
            candidates.append((venue_match.start(), venue, city))
    if not candidates:
        return None
    _, venue, city = min(candidates)
    return venue, city


def parse_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    url = post.get('link')
    description = event_text(post.get('content', {}).get('rendered'))
    if not title or not url or not description:
        return []

    records = []
    seen = set()
    matches = list(DATE_TIME_RE.finditer(description))
    matches.extend(MONTH_DATE_TIME_RE.finditer(description))
    for match in sorted(matches, key=lambda item: item.start()):
        location = venue_after(description, match)
        if not location:
            continue
        try:
            event_date = date(
                int(match.group('year')),
                (
                    int(match.groupdict().get('month'))
                    if match.groupdict().get('month')
                    else MONTHS[match.group('month_name').lower()]
                ),
                int(match.group('day')),
            ).isoformat()
        except ValueError:
            continue

        time_from = (
            f"{int(match.group('hour')):02d}:"
            f"{match.group('minute') or '00'}"
        )
        venue, city = location
        identity = (event_date, time_from, venue)
        if identity in seen:
            continue
        seen.add(identity)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'EE',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def fetch_category(category_id):
    params = {
        'categories': category_id,
        'per_page': 100,
        'page': 1,
        '_fields': 'id,link,title,content',
    }
    posts = []
    while True:
        response = requests.get(API_URL, params=params, headers=HEADERS, timeout=45)
        response.raise_for_status()
        page = response.json()
        posts.extend(page)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if params['page'] >= total_pages:
            return posts
        params['page'] += 1


def get_concerts():
    records = []
    skipped_count = 0
    try:
        for category_id in EVENT_CATEGORIES:
            for post in fetch_category(category_id):
                parsed = parse_post(post)
                if not parsed:
                    skipped_count += 1
                records.extend(parsed)
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Failed to fetch Pärnu Linnaorkester concerts',
            event='crawler_fetch_failed',
            level='error',
            url=API_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    if skipped_count:
        log_message(
            'Skipped archive posts without a complete date and known venue',
            event='crawler_items_skipped',
            level='info',
            url=API_URL,
            record_count=skipped_count,
        )

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'], record['venue'])
        unique[key] = record
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'], record['venue'], record['title']),
    )


class ParnuorkesterEeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='parnuorkester_ee',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
        upload_target='classical',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    ParnuorkesterEeCrawler().run()


if __name__ == '__main__':
    main()
