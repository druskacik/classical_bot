import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://chambermusictulsa.org/'
SOURCE = 'Chamber Music Tulsa'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
CITY = 'Tulsa'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Za-z]+\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2}(?::\d{2})?)\s*([ap])\.?\s*m\.?\s*', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time_and_venue(value):
    text = clean_text(value).replace('\n', ' ')
    text = DATE_RE.sub('', text).strip(' ,-')
    match = TIME_RE.search(text)
    if not match:
        return None, None

    try:
        time_from = datetime.strptime(
            f'{match.group(1)} {match.group(2)}M',
            '%I:%M %p' if ':' in match.group(1) else '%I %p',
        ).strftime('%H:%M')
    except ValueError:
        return None, None

    venue = text[match.end():].strip(' ,-')
    venue = re.sub(r'^Performance\s*,\s*', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+Reception immediately following.*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+w/David Shifrin.*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+4200 S Atlanta Pl.*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+101 E Archer St.*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+308 S\. Lansing Ave\..*$', '', venue, flags=re.IGNORECASE)
    venue = re.sub(r'\s+108 N\. Detroit Ave\..*$', '', venue, flags=re.IGNORECASE)
    venue = venue.strip(' ,-')
    if venue.upper() in {'TBA', 'TBD'}:
        venue = ''
    return time_from, venue or None


def get_all(session, endpoint, fields):
    records = []
    page = 1
    while True:
        response = session.get(
            f'{API_URL}/{endpoint}',
            params={
                'per_page': 100,
                'page': page,
                'orderby': 'id',
                'order': 'asc',
                '_fields': fields,
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        records.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            return records
        page += 1


def parse_archive(html, post_urls):
    soup = BeautifulSoup(html, 'html.parser')
    records = []

    for article in soup.select('article.season-listing'):
        heading = article.select_one('h1, h2.entry-title, h2')
        title = clean_text(heading)
        class_names = article.get('class', [])
        post_class = next((item for item in class_names if re.fullmatch(r'post-\d+', item)), '')
        post_id = int(post_class[5:]) if post_class else None
        url = post_urls.get(post_id)
        if not title or not url:
            continue

        for schedule in article.select('.day-schedule'):
            date_venue = schedule.select_one('.date-venue-wrap')
            event_date = parse_date(date_venue)
            time_from, venue = parse_time_and_venue(date_venue)
            if not event_date or not venue:
                continue

            details = schedule.select_one('.listing-day-details')
            if details:
                for unwanted in details.select('.ticket-link-wrap, a'):
                    unwanted.decompose()
            description = clean_text(details) or None

            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': time_from,
                'venue': venue,
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    seasons = get_all(session, 'season', 'id,slug,name')
    posts = get_all(session, 'season-listing', 'id,link,season')
    post_urls = {post['id']: post['link'] for post in posts}

    records = []
    for season in seasons:
        archive_url = f"{SOURCE_URL}season/{season['slug']}/"
        try:
            response = session.get(archive_url, timeout=45)
            response.raise_for_status()
            records.extend(parse_archive(response.text, post_urls))
        except requests.RequestException as error:
            log_message(
                'Season archive request failed',
                event='crawler_archive_request_failed',
                level='warning',
                url=archive_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {}
    for record in records:
        key = (record['url'], record['date'], record['time_from'], record['venue'])
        unique[key] = record

    result = sorted(
        unique.values(),
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )
    if not result:
        log_message(
            'No concert performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class ChamberMusicTulsaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusictulsa_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ChamberMusicTulsaOrgCrawler().run()


if __name__ == '__main__':
    main()
