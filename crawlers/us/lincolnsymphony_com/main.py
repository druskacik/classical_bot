import base64
import hashlib
import random
import re
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://lincolnsymphony.com/'
SOURCE = "Lincoln's Symphony Orchestra"
CITY = 'Lincoln'
LISTING_URLS = (
    urljoin(SOURCE_URL, 'events-and-single-tickets/'),
    urljoin(SOURCE_URL, 'past-events/'),
)

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December'
)
DATE_RE = re.compile(rf'\b({MONTH_PATTERN})\s+(\d{{1,2}})\b', re.I)
YEAR_RE = re.compile(r'\b(20\d{2})\b')
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([AP]M)\b', re.I)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def solve_siteground_challenge(session, response):
    refresh = re.search(r'content="0;([^"]+)', response.text)
    if not refresh:
        return response

    challenge_response = session.get(
        urljoin(response.url, refresh.group(1)), timeout=45
    )
    challenge_response.raise_for_status()
    match = re.search(r'const sgchallenge="([^"]+)', challenge_response.text)
    if not match:
        return response

    challenge = match.group(1)
    complexity = int(challenge.split(':', 1)[0])
    start_from = random.randrange(5_000_000)
    started = time.monotonic()

    for counter in range(start_from, start_from + 40_000_000):
        counter_bytes = counter.to_bytes(max(1, (counter.bit_length() + 7) // 8), 'big')
        candidate = challenge.encode() + counter_bytes
        candidate += b'\0' * (-len(candidate) % 4)
        digest = int.from_bytes(hashlib.sha1(candidate).digest()[:4], 'big')
        if digest >> (32 - complexity) == 0:
            break
    else:
        raise RuntimeError('SiteGround challenge solution was not found')

    elapsed_ms = int((time.monotonic() - started) * 1000)
    solution_url = urljoin(response.url, '/.well-known/sgcaptcha/')
    solved = session.get(
        solution_url,
        params={
            'r': '/',
            'sol': base64.b64encode(candidate).decode(),
            's': f'{elapsed_ms}:{counter - start_from}',
        },
        timeout=45,
    )
    solved.raise_for_status()
    return session.get(response.url, timeout=45)


def get_page(session, url):
    response = session.get(url, timeout=45)
    if response.status_code == 202 and response.headers.get('SG-Captcha') == 'challenge':
        response = solve_siteground_challenge(session, response)
    response.raise_for_status()
    return response


def parse_occurrences(value):
    text = clean_text(value)
    year_match = YEAR_RE.search(text)
    if not year_match:
        return []

    dates = []
    for month, day in DATE_RE.findall(text):
        try:
            dates.append(
                datetime.strptime(
                    f'{month} {day} {year_match.group(1)}', '%B %d %Y'
                ).date().isoformat()
            )
        except ValueError:
            continue

    times = []
    for hour, minute, meridiem in TIME_RE.findall(text):
        try:
            times.append(
                datetime.strptime(
                    f'{hour}:{minute or "00"} {meridiem.upper()}', '%I:%M %p'
                ).strftime('%H:%M')
            )
        except ValueError:
            continue

    if len(dates) == 1:
        return [(dates[0], event_time) for event_time in (times or [None])]
    if len(dates) == len(times):
        return list(zip(dates, times))
    return [(event_date, times[0] if times else None) for event_date in dates]


def detail_description(session, url):
    try:
        soup = BeautifulSoup(get_page(session, url).text, 'html.parser')
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    content = soup.select_one('#main-content')
    if not content:
        return None
    for node in content.select('script, style, nav, form'):
        node.decompose()
    return clean_text(content.get_text('\n', strip=True)) or None


def api_descriptions(session, post_ids):
    if not post_ids:
        return {}
    api_url = urljoin(SOURCE_URL, 'wp-json/wp/v2/posts')
    try:
        response = get_page(session, api_url + '?' + requests.compat.urlencode({
            'include': ','.join(post_ids),
            'per_page': 100,
            '_fields': 'link,content',
        }))
        posts = response.json()
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Concert API request failed',
            event='crawler_api_failed',
            level='warning',
            url=api_url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return {}

    descriptions = {}
    for post in posts:
        url = post.get('link')
        rendered = post.get('content', {}).get('rendered', '')
        if not url or not rendered:
            continue
        soup = BeautifulSoup(rendered, 'html.parser')
        for node in soup.select('script, style, nav, form'):
            node.decompose()
        descriptions[url.rstrip('/') + '/'] = clean_text(
            soup.get_text('\n', strip=True)
        ) or None
    return descriptions


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    cards = {}
    post_ids = []

    for listing_url in LISTING_URLS:
        response = get_page(session, listing_url)
        soup = BeautifulSoup(response.text, 'html.parser')
        for card in soup.select('article.et_pb_post'):
            title_link = card.select_one('h2.entry-title a[href]')
            summary = card.select_one('.post-content-inner')
            if not title_link or not summary:
                continue
            title = clean_text(title_link.get_text(' ', strip=True))
            url = urljoin(listing_url, title_link.get('href'))
            lines = [clean_text(line) for line in summary.get_text('\n').splitlines()]
            lines = [line for line in lines if line]
            if not title or not lines or not url.startswith(SOURCE_URL):
                continue
            occurrences = parse_occurrences(lines[0])
            venue = clean_text(lines[1]) if len(lines) > 1 else ''
            if not occurrences or not venue:
                continue
            cards[url] = (title, venue, occurrences)

            post_id = re.search(r'\bpost-(\d+)\b', ' '.join(card.get('class', [])))
            if post_id:
                post_ids.append(post_id.group(1))

    descriptions = api_descriptions(session, post_ids)
    records = []
    for url, (title, venue, occurrences) in cards.items():
        description = descriptions.get(url.rstrip('/') + '/')
        if description is None:
            description = detail_description(session, url)
        for event_date, time_from in occurrences:
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

    if not records:
        log_message(
            'No concert cards found',
            event='crawler_empty_listing',
            level='warning',
            url=LISTING_URLS[0],
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LincolnSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lincolnsymphony_com',
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
    LincolnSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
