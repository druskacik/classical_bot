import base64
import hashlib
import json
import random
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sfcmp.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
SOURCE = 'San Francisco Contemporary Music Players'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value)
    if '<' in text and '>' in text:
        text = BeautifulSoup(text, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
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
    challenge_match = re.search(
        r'const sgchallenge="([^"]+)', challenge_response.text
    )
    submit_match = re.search(
        r'const sgsubmit_url="([^"]+)', challenge_response.text
    )
    if not challenge_match or not submit_match:
        return response

    challenge = challenge_match.group(1)
    complexity = int(challenge.split(':', 1)[0])
    start_from = random.randrange(5_000_000)
    started = time.monotonic()

    for counter in range(start_from, start_from + 40_000_000):
        counter_bytes = counter.to_bytes(
            max(1, (counter.bit_length() + 7) // 8), 'big'
        )
        candidate = challenge.encode() + counter_bytes
        candidate += b'\0' * (-len(candidate) % 4)
        digest = int.from_bytes(hashlib.sha1(candidate).digest()[:4], 'big')
        if digest >> (32 - complexity) == 0:
            break
    else:
        raise RuntimeError('SiteGround challenge solution was not found')

    elapsed_ms = int((time.monotonic() - started) * 1000)
    solved = session.get(
        urljoin(response.url, submit_match.group(1)),
        params={
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


def iter_json_ld(soup):
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                yield item


def parse_start_date(value):
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None, None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def parse_event_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event = next(
        (
            item
            for item in iter_json_ld(soup)
            if item.get('@type') == 'MusicEvent' or item.get('type') == 'MusicEvent'
        ),
        None,
    )
    if event:
        title = clean_text(event.get('name'))
        event_date, time_from = parse_start_date(event.get('startDate'))
        location = event.get('location') if isinstance(event.get('location'), dict) else {}
        address = location.get('address') if isinstance(location.get('address'), dict) else {}
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        description = clean_text(event.get('description')) or None
        canonical_url = event.get('url') or url
    else:
        title_node = soup.select_one('main h1')
        date_node = soup.select_one('.event-dates-page .el-item h3')
        location_node = soup.select_one('.event-venue-page .event-location')
        title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')
        date_text = clean_text(date_node.get_text(' ', strip=True) if date_node else '')
        date_match = re.search(
            r'([A-Za-z]+),?\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+'
            r'(20\d{2})(?:\s+(\d{1,2}(?::\d{2})?\s*[ap]m))?',
            date_text,
            re.I,
        )
        event_date = None
        time_from = None
        if date_match:
            try:
                parsed = datetime.strptime(
                    ' '.join(date_match.group(2, 3, 4)), '%B %d %Y'
                )
                event_date = parsed.date().isoformat()
            except ValueError:
                pass
            if date_match.group(5):
                for pattern in ('%I:%M %p', '%I %p'):
                    try:
                        time_from = datetime.strptime(
                            date_match.group(5).upper(), pattern
                        ).strftime('%H:%M')
                        break
                    except ValueError:
                        pass

        location_lines = []
        if location_node:
            location_lines = [
                clean_text(line)
                for line in location_node.get_text('\n', strip=True).splitlines()
                if clean_text(line)
            ]
        venue = location_lines[0] if location_lines else ''
        city = ''
        for line in reversed(location_lines[1:]):
            city_match = re.match(r'(.+?),\s*[A-Z]{2}(?:\s+\d{5})?$', line)
            if city_match:
                city = clean_text(city_match.group(1))
                break

        details_heading = next(
            (
                node
                for node in soup.select('.event-main h2, .event-main h3')
                if clean_text(node.get_text()) == 'Details'
            ),
            None,
        )
        description = None
        if details_heading and details_heading.parent:
            details_container = details_heading.parent
            details_heading.extract()
            description = clean_text(details_container.get_text('\n', strip=True)) or None
        canonical_url = url
    if not all((title, event_date, canonical_url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': canonical_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def concert_urls(sitemap_text):
    soup = BeautifulSoup(sitemap_text, 'xml')
    urls = []
    for node in soup.find_all('loc'):
        url = clean_text(node.get_text())
        path_parts = [part for part in urlparse(url).path.split('/') if part]
        if len(path_parts) == 3 and path_parts[0] == 'concerts':
            urls.append(url)
    return urls


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    sitemap = get_page(session, SITEMAP_URL)
    urls = concert_urls(sitemap.text)
    records = []

    for url in urls:
        try:
            response = get_page(session, url)
            record = parse_event_page(response.text, url)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)
        else:
            log_message(
                'Concert detail could not be parsed',
                event='crawler_detail_unparseable',
                level='warning',
                url=url,
            )

    if not records:
        log_message(
            'No concert events found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title']),
    )


class SfcmpOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sfcmp_org',
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
    SfcmpOrgCrawler().run()


if __name__ == '__main__':
    main()
