import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://newlondonoperagroup.org/'
SOURCE = 'The New London Opera Group'
API_URL = 'https://public-api.wordpress.com/rest/v1.1/sites/newlondonoperagroup.org/posts/'

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        1,
    )
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_PATTERN = re.compile(
    rf'(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{MONTH_PATTERN})'
    rf'(?:\s+(?P<year>20\d{{2}}))?',
    re.I,
)
TIME_PATTERN = re.compile(r'\b(?:at\s+)?(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.I)

VENUES = {
    'riverhead theatre': ('Riverhead Theatre', 'Louth'),
    "louth playgoers’ riverhead theatre": ('Riverhead Theatre', 'Louth'),
    "louth playgoers' riverhead theatre": ('Riverhead Theatre', 'Louth'),
    'holy trinity church': ('Holy Trinity Church, Prince Consort Road', 'London'),
    'conway hall': ('Conway Hall', 'London'),
    'harrogate theatre': ('Harrogate Theatre', 'Harrogate'),
    'buxton opera house': ('Buxton Opera House', 'Buxton'),
}

NON_EVENT_TITLE_MARKERS = (
    'audition', 'cast announced', 'cast announcement', 'casting', 'review',
    'award', 'join the chorus', 'director', 'would you like to direct', 'intro meeting',
    'announcement from the board',
)
EVENT_TITLE_MARKERS = (
    'ticket', 'on sale', 'booking now', 'coming', 'concert', 'production announced',
    'performance dates', 'in louth',
)


def clean_text(value):
    text = BeautifulSoup(str(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def api_pages(session):
    params = {'number': 100}
    while True:
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        payload = response.json()
        yield payload
        cursor = (payload.get('meta') or {}).get('next_page')
        if not cursor:
            break
        params = {'number': 100, 'page_handle': cursor}


def find_venue(text, position):
    folded = text.casefold()
    choices = []
    for marker, location in VENUES.items():
        start = 0
        while True:
            found = folded.find(marker, start)
            if found < 0:
                break
            choices.append((abs(found - position), found, location))
            start = found + len(marker)
    if not choices:
        return None
    _, _, location = min(choices)
    return location


def infer_year(post, match, text):
    if match.group('year'):
        return int(match.group('year'))
    nearby = text[match.end():match.end() + 120]
    trailing = re.search(r'\b(20\d{2})\b', nearby)
    if trailing:
        return int(trailing.group(1))
    published = datetime.fromisoformat(post['date'].replace('Z', '+00:00')).date()
    month = MONTHS[match.group('month').casefold()]
    # Event announcements near year-end commonly advertise the following spring.
    return published.year + (1 if month + 3 < published.month else 0)


def nearby_time(text, match):
    window = text[match.end():match.end() + 180]
    found = TIME_PATTERN.search(window)
    if not found:
        return None
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    if found.group(3).casefold() == 'pm' and hour != 12:
        hour += 12
    if found.group(3).casefold() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None


def event_title(post, soup):
    raw = clean_text(post.get('title'))
    title = re.split(
        r'\s+[–—-]\s+(?:tickets?|booking|coming|cast|auditions?|news|performance dates).*$',
        raw,
        maxsplit=1,
        flags=re.I,
    )[0].strip(' –—-!')
    if title.casefold().startswith(('an important update', 'announcing our')):
        candidates = []
        for node in soup.select('em, strong'):
            candidate = clean_text(node)
            if 3 < len(candidate) < 100 and not re.search(r'performance|date|thursday|friday', candidate, re.I):
                position = clean_text(soup).find(candidate)
                dates_position = clean_text(soup).casefold().find('performance dates')
                if 0 <= position < dates_position:
                    candidates.append((position, candidate))
        if candidates:
            title = max(candidates)[1]
    return title


def is_candidate_post(post, text):
    title = clean_text(post.get('title')).casefold()
    if any(marker in title for marker in NON_EVENT_TITLE_MARKERS):
        return False
    if title.startswith('announcing our'):
        return False
    if any(marker in title for marker in EVENT_TITLE_MARKERS):
        return True
    return bool(re.search(
        r'\b(?:performances? (?:will|are|take)|performance dates are|concert (?:on|at)|give .*? performance)\b',
        text,
        re.I,
    ))


def records_from_post(post):
    soup = BeautifulSoup(post.get('content') or '', 'html.parser')
    text = clean_text(soup)
    if not is_candidate_post(post, text):
        return []
    title = event_title(post, soup)
    url = (post.get('URL') or '').replace('http://newlondonoperagroup.org/', SOURCE_URL)
    records = []
    for match in DATE_PATTERN.finditer(text):
        context = text[max(0, match.start() - 350):match.end() + 350].casefold()
        local_context = text[max(0, match.start() - 100):match.end() + 100].casefold()
        if not re.search(r'perform|concert|production|ticket|on sale|coming|book', context):
            continue
        if re.search(r'\bas of\s*$', text[max(0, match.start() - 25):match.start()], re.I):
            continue
        if re.search(r'audition|rehearsal|meeting|cast required|application|deadline', local_context) and not re.search(
            r'performances? (?:will|are|take)|performance dates are|concert (?:on|at)|tickets? (?:are )?on sale',
            local_context,
        ):
            continue
        venue_location = find_venue(text, match.start())
        if not venue_location:
            continue
        venue, city = venue_location
        days = [int(match.group('day'))]
        prefix = text[max(0, match.start() - 100):match.start()]
        if not re.search(rf'\b(?:{MONTH_PATTERN})\b', prefix, re.I):
            for day in re.findall(
                r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+(\d{1,2})(?:st|nd|rd|th)?\b',
                prefix,
                flags=re.I,
            ):
                if int(day) not in days:
                    days.insert(0, int(day))
        for day in days:
            try:
                event_date = datetime(
                    infer_year(post, match, text),
                    MONTHS[match.group('month').casefold()],
                    day,
                ).date().isoformat()
            except ValueError:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': nearby_time(text, match),
                'venue': venue,
                'city': city,
                'country_code': 'GB',
                'description': text or None,
            })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for payload in api_pages(session):
        for post in payload.get('posts') or []:
            try:
                records.extend(records_from_post(post))
            except (KeyError, TypeError, ValueError) as error:
                log_message(
                    'Failed to parse New London Opera Group post',
                    event='crawler_item_failed',
                    level='warning',
                    url=post.get('URL'),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    return sorted(
        unique.values(),
        key=lambda record: (record['date'], record['time_from'] or '', record['title']),
    )


class NewLondonOperaGroupOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='newlondonoperagroup_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NewLondonOperaGroupOrgCrawler().run()


if __name__ == '__main__':
    main()
