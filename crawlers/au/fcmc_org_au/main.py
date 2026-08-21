import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://fcmc.org.au/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Foundation for Contemporary Music & Culture'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|'
    r'October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
)
DATE_RE = re.compile(
    rf'(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{MONTH_PATTERN})\s*,?\s*(?P<year>20\d{{2}})',
    re.I,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', re.I)
TIME_RANGE_RE = re.compile(
    r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*[-]\s*'
    r'\d{1,2}(?::\d{2})?\s*(am|pm)\b',
    re.I,
)


def clean_text(value):
    text = BeautifulSoup(html.unescape(value or ''), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u2013', '-').replace('\u2014', '-')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('day')} {match.group('month')} {match.group('year')}",
            '%d %B %Y' if len(match.group('month')) > 3 else '%d %b %Y',
        ).date().isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = TIME_RANGE_RE.search(text)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == 'pm':
            hour += 12
        return f'{hour:02d}:{int(match.group(2) or 0):02d}'
    match = TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'pm':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def parse_location(text, title):
    lowered = text.lower()
    if 'visions gallery' in lowered and 'brisbane' in lowered:
        return 'Visions Gallery', 'Brisbane'
    if 'cockatoo island' in lowered and 'sydney' in lowered:
        return 'Building 15, Cockatoo Island', 'Sydney'
    if ('new improvised music series' in title.lower() and
            ('paddington' in lowered or 'substation' in lowered) and
            'brisbane' in lowered):
        return 'The Substation', 'Brisbane'
    return None, None


def parse_post(post):
    # The events post is an overview which duplicates detail posts and embeds
    # unrelated third-party listings; concrete first-party posts are parsed below.
    if post.get('slug') == 'events':
        return None
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    url = post.get('link', '').strip()
    event_date = parse_date(description)
    venue, city = parse_location(description, title)
    if not title or not url or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': 'AU',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class FcmcOrgAuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fcmc_org_au',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AU',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            API_URL,
            params={
                'per_page': 100,
                'page': 1,
                '_fields': 'id,slug,link,title,content',
            },
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        posts = response.json()
        records = []
        for post in posts:
            record = parse_post(post)
            if record:
                records.append(record)
        log_message(
            'FCMC archive parsed',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FcmcOrgAuCrawler().run()


if __name__ == '__main__':
    main()
