import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tcbo.it/'
CALENDAR_URL = f'{SOURCE_URL}calendario-eventi/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
SOURCE = 'Teatro Comunale di Bologna'
CALENDAR_CATEGORY_ID = 144

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

MONTHS = {
    'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
    'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
    'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mag': 5, 'giu': 6,
    'lug': 7, 'ago': 8, 'sep': 9, 'set': 9, 'ott': 10,
    'nov': 11, 'dec': 12, 'dic': 12,
}

MONTH_PATTERN = '|'.join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERN = re.compile(
    rf'\b(?P<day>\d{{1,2}})\s*(?P<month>{MONTH_PATTERN})\b', re.I
)
TIME_PATTERN = re.compile(r'\b(?:h\.?|ore)?\s*(\d{1,2})[.:](\d{2})\b', re.I)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'\[(?:/?[a-z_][^\]]*)\]', '\n', text, flags=re.I)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_json(session, endpoint, params=None):
    response = session.get(f'{API_URL}/{endpoint}', params=params, timeout=60)
    response.raise_for_status()
    return response


def valid_time(value):
    match = TIME_PATTERN.search(value or '')
    if not match:
        return None
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        return None
    return f'{hour:02d}:{minute:02d}'


def description_from_content(rendered):
    # Every detail page appends a copy of the live calendar. It describes other
    # events and must not leak into this event's programme text.
    rendered = rendered.split('<div class="eventCalendarFilter"', 1)[0]
    text = clean_text(BeautifulSoup(rendered, 'html.parser'))
    return text or None


def current_occurrences(session):
    # The calendar page is sometimes served from a stale WordPress cache without
    # its cards. The home page has the same first-party deck and is populated.
    response = session.get(SOURCE_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    occurrences = []
    for card in soup.select('.eventCalendarWrap'):
        link = card.select_one('a[href*="/eventi/"]')
        day_node = card.select_one('.eventCalendarDay')
        month_node = card.select_one('.eventCalendarMonth')
        title_node = card.select_one('.eventCalendarTitle')
        place_node = card.select_one('.eventCalendarPlace')
        if not all((link, day_node, month_node, title_node, place_node)):
            continue
        month = MONTHS.get(clean_text(month_node).casefold())
        try:
            day = int(clean_text(day_node))
        except ValueError:
            continue
        if month is None:
            continue
        today = date.today()
        year = today.year + ((month, day) < (today.month, today.day))
        place = clean_text(place_node)
        venue = re.split(r',\s*\d{1,2}[.:]\d{2}\b', place, maxsplit=1)[0].strip()
        try:
            event_date = date(year, month, day).isoformat()
        except ValueError:
            continue
        if venue:
            occurrences.append({
                'title': clean_text(title_node), 'date': event_date,
                'url': link.get('href'), 'time_from': valid_time(place),
                'venue': venue, 'city': 'Bologna',
            })
    return occurrences


def year_terms(session):
    terms = {}
    page = 1
    while True:
        response = get_json(session, 'portfolio_category', {'per_page': 100, 'page': page})
        for term in response.json():
            if re.fullmatch(r'20\d{2}', term['name']):
                terms[term['id']] = int(term['name'])
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            return terms
        page += 1


def portfolio_posts(session):
    posts = []
    page = 1
    while True:
        response = get_json(session, 'portfolio', {
            'portfolio_category': CALENDAR_CATEGORY_ID,
            'per_page': 100,
            'page': page,
            '_fields': 'id,link,title,content,portfolio_category',
        })
        posts.extend(response.json())
        if page >= int(response.headers.get('X-WP-TotalPages', '1')):
            return posts
        page += 1


def archive_occurrences(post, year):
    rendered = post['content']['rendered'].split('<div class="eventCalendarFilter"', 1)[0]
    soup = BeautifulSoup(rendered, 'html.parser')
    title = clean_text(BeautifulSoup(post['title']['rendered'], 'html.parser'))
    text = clean_text(soup)
    lines = [line for line in text.splitlines() if line]
    records = []
    for index, line in enumerate(lines):
        matches = list(DATE_PATTERN.finditer(line))
        if not matches:
            continue
        context = ' | '.join(lines[index:index + 3])
        time_from = valid_time(context)
        venue = None
        parts = [part.strip(' –-|') for part in re.split(r'\||\n', context)]
        for part in parts:
            if DATE_PATTERN.search(part) or TIME_PATTERN.search(part):
                continue
            if re.search(r'\b(teatro|auditorium|conservatorio|sala|basilica|chiesa|palazzo|arena|chiostro|cortile)\b', part, re.I):
                venue = part
                break
        if venue is None:
            venue_match = re.search(
                r'\b(?:Teatro|Auditorium|Conservatorio|Sala|Basilica|Chiesa|Palazzo|Arena|Chiostro|Cortile)\b[^|\n]{2,80}',
                context, re.I,
            )
            venue = venue_match.group(0).strip(' –-|') if venue_match else None
        if venue:
            venue = re.sub(r"^(?:all['’]|al|nel|nella)\s*", '', venue, flags=re.I)
        if (
            not venue or len(venue) < 6 or venue.casefold() == 'teatro'
            or re.search(
                r'\b(?:orchestra|propone|nuova sede|bigliett|piazza della costituzione)\b',
                venue, re.I,
            )
        ):
            continue
        for match in matches:
            try:
                event_date = date(
                    year, MONTHS[match.group('month').casefold()], int(match.group('day'))
                ).isoformat()
            except ValueError:
                continue
            records.append({
                'title': title, 'date': event_date, 'url': post['link'],
                'time_from': time_from, 'venue': venue, 'city': 'Bologna',
            })
    return records


class TcboItCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tcbo_it',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IT',
        upload_target='potential',
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
            current = current_occurrences(session)
            terms = year_terms(session)
            posts = portfolio_posts(session)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch TCBO calendar', event='crawler_fetch_failed',
                level='error', url=CALENDAR_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        post_by_url = {post['link']: post for post in posts}
        records = []
        current_keys = set()
        for occurrence in current:
            post = post_by_url.get(occurrence['url'])
            description = description_from_content(post['content']['rendered']) if post else None
            record = {**occurrence, 'description': description,
                      'country_code': 'IT', 'source_url': SOURCE_URL, 'source': SOURCE}
            records.append(record)
            current_keys.add((record['url'], record['date']))

        for post in posts:
            years = sorted({terms[term] for term in post['portfolio_category'] if term in terms})
            if not years:
                continue
            description = description_from_content(post['content']['rendered'])
            for occurrence in archive_occurrences(post, years[-1]):
                if (occurrence['url'], occurrence['date']) in current_keys:
                    continue
                records.append({
                    **occurrence, 'description': description,
                    'country_code': 'IT', 'source_url': SOURCE_URL, 'source': SOURCE,
                })

        unique = {}
        for record in records:
            key = (record['title'], record['date'], record['time_from'], record['venue'])
            unique[key] = record
        return sorted(unique.values(), key=lambda row: (
            row['date'], row['time_from'] or '', row['title'], row['venue']
        ))


def main():
    TcboItCrawler().run()


if __name__ == '__main__':
    main()
