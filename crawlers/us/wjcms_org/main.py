import base64
import hashlib
import html
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wjcms.org/'
SOURCE = 'West Jersey Chamber Music Society'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/eventbrite_events')
PER_PAGE = 100

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html;q=0.9',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_RE = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December'
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def challenge_location(response):
    if response.status_code != 202:
        return None
    match = re.search(r'<meta[^>]+content=["\']0;([^"\']+)', response.text, re.I)
    return urljoin(response.url, html.unescape(match.group(1))) if match else None


def solve_siteground_challenge(session, response):
    challenge_url = challenge_location(response)
    if not challenge_url:
        return False

    page = session.get(challenge_url, timeout=45)
    page.raise_for_status()
    challenge_match = re.search(r'const sgchallenge="([^"]+)"', page.text)
    submit_match = re.search(r'const sgsubmit_url="([^"]+)"', page.text)
    if not challenge_match or not submit_match:
        return False

    challenge = challenge_match.group(1)
    complexity = int(challenge.split(':', 1)[0])
    seed = challenge.encode('utf-8')
    counter = random.randrange(5_000_000)
    started = time.monotonic()

    for hashes in range(40_000_001):
        counter_bytes = counter.to_bytes(max(1, (counter.bit_length() + 7) // 8), 'big')
        solution = seed + counter_bytes
        solution += b'\0' * (-len(solution) % 4)
        digest_prefix = int.from_bytes(hashlib.sha1(solution).digest()[:4], 'big')
        if digest_prefix >> (32 - complexity) == 0:
            break
        counter += 1
    else:
        raise RuntimeError('SiteGround challenge solution was not found')

    elapsed_ms = int((time.monotonic() - started) * 1000)
    solved = session.get(
        urljoin(page.url, submit_match.group(1)),
        params={
            'sol': base64.b64encode(solution).decode('ascii'),
            's': f'{elapsed_ms}:{hashes}',
        },
        timeout=45,
    )
    return solved.status_code in (200, 202) and '_I_' in session.cookies


def get_response(session, url, *, params=None):
    response = session.get(url, params=params, timeout=45)
    if response.status_code == 202:
        if not solve_siteground_challenge(session, response):
            raise RuntimeError('Unable to pass the source website challenge')
        response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def labeled_paragraph(soup, label):
    marker = soup.find(
        lambda tag: tag.name in {'strong', 'b'}
        and clean_text(tag).rstrip(':').casefold() == label.casefold()
    )
    if not marker:
        return ''
    value = marker.find_next('p')
    return clean_text(value)


def infer_date(date_text, description, imported_at):
    date_text = date_text or description
    match = re.search(rf'\b({MONTH_RE})\s+(\d{{1,2}})(?:,?\s+(20\d{{2}}))?', date_text, re.I)
    if not match:
        return None

    month, day, year = match.groups()
    if not year:
        explicit = re.search(
            rf'\b{re.escape(month)}\s+{int(day)}(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b',
            description,
            re.I,
        )
        year = explicit.group(1) if explicit else None

    years = [int(year)] if year else [imported_at.year, imported_at.year + 1]
    candidates = []
    for candidate_year in years:
        try:
            candidates.append(datetime.strptime(
                f'{month} {day} {candidate_year}', '%B %d %Y'
            ).date())
        except ValueError:
            continue
    if not candidates:
        return None

    if year:
        return candidates[0].isoformat()
    threshold = imported_at.date() - timedelta(days=45)
    future = [candidate for candidate in candidates if candidate >= threshold]
    return (min(future) if future else min(candidates)).isoformat()


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', value, re.I)
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    try:
        return datetime.strptime(
            f'{hour}:{minute or "00"} {meridiem.upper()}M', '%I:%M %p'
        ).strftime('%H:%M')
    except ValueError:
        return None


def title_key(value):
    return re.sub(r'[^a-z0-9]+', ' ', clean_text(value).casefold()).strip()


def parse_event(session, event, archive_dates=None):
    classes = set(event.get('class_list') or [])
    if 'eventbrite_category-season-tickets' in classes:
        return None

    title = clean_text((event.get('title') or {}).get('rendered'))
    url = event.get('link')
    description = clean_text((event.get('content') or {}).get('rendered')) or None
    if not title or not url:
        return None

    try:
        imported_at = datetime.fromisoformat(event.get('date', ''))
        soup = BeautifulSoup(get_response(session, url).text, 'html.parser')
    except (ValueError, requests.RequestException, RuntimeError) as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None

    event_date = infer_date(labeled_paragraph(soup, 'Date'), description or '', imported_at)
    if not event_date:
        event_date = (archive_dates or {}).get(title_key(title))
    venue_container = soup.select_one('.venue')
    venue_paragraphs = venue_container.find_all('p') if venue_container else []
    venue = clean_text(venue_paragraphs[0]) if venue_paragraphs else ''
    city = ''
    for paragraph in venue_paragraphs[1:]:
        value = clean_text(paragraph)
        city_match = re.search(r'\b([^,\n]+),\s*[A-Z]{2},\s*US\b', value)
        if city_match:
            city = clean_text(city_match.group(1))
            break

    if not all((event_date, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(labeled_paragraph(soup, 'Time')),
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    archive_response = get_response(
        session,
        urljoin(SOURCE_URL, 'wp-json/wp/v2/past_concerts'),
        params={'per_page': PER_PAGE, '_fields': 'date,title'},
    )
    archive_dates = {
        title_key((item.get('title') or {}).get('rendered')): item.get('date', '')[:10]
        for item in archive_response.json()
    }
    page = 1
    total_pages = 1

    while page <= total_pages:
        response = get_response(session, API_URL, params={
            'per_page': PER_PAGE,
            'page': page,
            'status': 'publish',
            '_fields': 'id,date,link,title,content,class_list',
        })
        total_pages = int(response.headers.get('X-WP-TotalPages') or 1)
        for event in response.json():
            record = parse_event(session, event, archive_dates)
            if record:
                records.append(record)
        page += 1

    log_message(
        'WJCMS API scrape completed',
        event='crawler_api_scrape_completed',
        record_count=len(records),
    )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class WjcmsOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wjcms_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    WjcmsOrgCrawler().run()


if __name__ == '__main__':
    main()
