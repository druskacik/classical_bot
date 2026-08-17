import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://pocketopera.org/'
SOURCE = 'Pocket Opera'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|Feburary|March|April|May|June|July|August|'
    'September|October|November|December'
)
DATE_LINE_RE = re.compile(
    rf'^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    rf'(?P<month>{MONTHS}),?\s*(?P<day>\d{{1,2}})'
    rf'(?:,\s*(?P<year>\d{{4}}))?'
    rf'(?:\s*(?:\||@)\s*(?P<time>\d{{1,2}}(?::\d{{2}})?\s*[ap]m))?\s*-?$',
    re.IGNORECASE,
)
SEASON_RE = re.compile(r'/((?:19|20)\d{2})(?:-season)?/')

VENUE_RULES = (
    (re.compile(r'mountain view center', re.I), 'Mountain View Center for the Performing Arts', 'Mountain View'),
    (re.compile(r'(?:gunn theater.*)?legion of honor', re.I), 'Gunn Theater at the Legion of Honor', 'San Francisco'),
    (re.compile(r'hillside club', re.I), 'Hillside Club', 'Berkeley'),
    (re.compile(r'jarvis conservatory', re.I), 'Jarvis Conservatory', 'Napa'),
    (re.compile(r'oshman family jcc', re.I), 'Oshman Family JCC', 'Palo Alto'),
)


def clean_text(value):
    if not value:
        return ''
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u2009', ' ')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def api_items(session, endpoint, params=None):
    params = dict(params or {})
    params.setdefault('per_page', 100)
    response = session.get(f'{API_URL}/{endpoint}', params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def production_urls(session):
    categories = api_items(session, 'categories', {'_fields': 'id,name'})
    season_ids = {
        item['id'] for item in categories
        if re.fullmatch(r'(?:19|20)\d{2} Season', html.unescape(item['name']))
    }

    posts = api_items(session, 'posts', {'_fields': 'link,categories'})
    urls = {
        item['link'] for item in posts
        if season_ids.intersection(item.get('categories', []))
    }

    # Before 2022, production detail entries were WordPress pages rather than
    # categorized posts. Only pages with a production year in the slug have
    # occurrence-level concert data; season overview pages are excluded.
    pages = api_items(session, 'pages', {'_fields': 'link,slug'})
    urls.update(
        item['link'] for item in pages
        if re.search(r'-(?:19|20)\d{2}$', item.get('slug', ''))
    )
    return sorted(urls)


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\s+', ' ', value.strip().upper())
    normalized = re.sub(r'(?<=\d)(AM|PM)$', r' \1', normalized)
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(normalized, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def page_year(url):
    match = SEASON_RE.search(url)
    return int(match.group(1)) if match else None


def parse_date(match, default_year):
    year = int(match.group('year') or default_year or 0)
    month = match.group('month').title().replace('Feburary', 'February')
    try:
        return datetime.strptime(
            f'{month} {match.group("day")} {year}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def venue_from_lines(lines):
    candidate = clean_text(' '.join(lines[:2])).rstrip(' >')
    for pattern, venue, city in VENUE_RULES:
        if pattern.search(candidate):
            return venue, city
    return None, None


def parse_production(html_text, url):
    soup = BeautifulSoup(html_text, 'html.parser')
    content = soup.select_one('#main-content, main, article')
    if not content:
        return []

    title_node = content.select_one('h1') or soup.select_one('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
    lines = [clean_text(line) for line in content.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    description = clean_text(content.get_text('\n', strip=True)) or None
    default_year = page_year(url)

    records = []
    for index, line in enumerate(lines):
        match = DATE_LINE_RE.fullmatch(line)
        if not match:
            continue

        event_date = parse_date(match, default_year)
        if not event_date:
            continue

        following = []
        for value in lines[index + 1:index + 6]:
            if DATE_LINE_RE.fullmatch(value):
                break
            if value.upper() in {'ADDED', 'CANCELLED'}:
                continue
            following.append(value)

        time_from = parse_time(match.group('time'))
        if not time_from and following:
            time_from = parse_time(following[0])
            if time_from:
                following = following[1:]

        venue, city = venue_from_lines(following)
        if not title or not venue or not city:
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    urls = production_urls(session)
    records = []

    for url in urls:
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_production(response.text, url))
        except requests.RequestException as error:
            log_message(
                'Production page request failed',
                event='crawler_page_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']))


class PocketOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='pocketopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    PocketOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
