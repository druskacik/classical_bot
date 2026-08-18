import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.limasymphony.com/'
SEASON_URL = urljoin(SOURCE_URL, '2026-2027-season')
SOURCE = 'Lima Symphony Orchestra'
DEFAULT_VENUE = 'Veterans Memorial Civic & Convention Center'
DEFAULT_CITY = 'Lima'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'[A-Za-z]+\s+\d{1,2},\s+\d{4})\s*//\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def season_detail_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.find_all('a', href=True):
        if clean_text(link.get_text(' ', strip=True)).lower() != 'learn more':
            continue
        url = urljoin(SEASON_URL, link['href'])
        if urlparse(url).netloc == urlparse(SOURCE_URL).netloc and url not in urls:
            urls.append(url)
    return urls


def description_from_page(soup, title):
    parts = []
    started = False
    for node in soup.select('h1, h2, h3, p'):
        text = clean_text(node.get_text(' ', strip=True))
        if not text:
            continue
        if node.name == 'h1' and title in text:
            started = True
            continue
        if not started:
            continue
        if text.lower() in {'newsletter', 'talk to us', 'quick links'}:
            break
        if DATE_TIME_RE.fullmatch(text) or text.lower().startswith(('buy ticket', 'buy lima', 'buy minster')):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.find('h1')
    title = clean_text(heading.get_text(' ', strip=True)) if heading else ''
    if not title:
        return []

    description = description_from_page(soup, title)
    records = []
    paragraphs = soup.find_all('p')
    for index, paragraph in enumerate(paragraphs):
        text = clean_text(paragraph.get_text(' ', strip=True))
        match = DATE_TIME_RE.fullmatch(text)
        if not match:
            continue

        event_date = parse_date(match.group('date'))
        if not event_date:
            continue

        venue = DEFAULT_VENUE
        city = DEFAULT_CITY
        for following_paragraph in paragraphs[index + 1:index + 6]:
            following = clean_text(following_paragraph.get_text(' ', strip=True))
            if DATE_TIME_RE.fullmatch(following):
                break
            if (
                following
                and len(following) <= 120
                and not following.endswith(('.', '!', '?'))
                and not DATE_TIME_RE.fullmatch(following)
                and ',' in following
            ):
                possible_venue, possible_city = following.rsplit(',', 1)
                if clean_text(possible_venue) and clean_text(possible_city):
                    venue = clean_text(possible_venue)
                    city = clean_text(possible_city)
                    break

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(match.group('time')),
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

    response = session.get(SEASON_URL, timeout=45)
    response.raise_for_status()
    detail_urls = season_detail_urls(response.text)
    if not detail_urls:
        log_message(
            'No season detail links found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
        return []

    records = []
    for url in detail_urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            records.extend(parse_detail(detail_response.text, url))
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class LimaSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='limasymphony_com',
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
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    LimaSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
