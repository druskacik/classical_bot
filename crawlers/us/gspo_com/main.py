import re
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gspo.com/'
SOURCE = 'Golden State Pops Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?P<date>(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*(?:at|[-–—])\s*'
    r'(?P<time>\d{1,2}(?::\d{2})?\s*[ap]m)',
    re.IGNORECASE,
)

VENUES = {
    'Redondo Union High School Auditorium': ('Redondo Beach', 'US'),
    'Redondo Beach Performing Arts Center': ('Redondo Beach', 'US'),
    'Warner Grand Theatre': ('San Pedro', 'US'),
    'Royce Hall': ('Los Angeles', 'US'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = DATE_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group('date'), '%A, %B %d, %Y').date().isoformat()
    except ValueError:
        return None, None
    compact_time = re.sub(r'\s+', '', match.group('time')).upper()
    for pattern in ('%I:%M%p', '%I%p'):
        try:
            return event_date, datetime.strptime(compact_time, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return event_date, None


def image_title(section):
    image = section.find('img', attrs={'data-src': True}) or section.find('img', src=True)
    if not image:
        return ''
    path = urlparse(image.get('data-src') or image.get('src')).path
    filename = unquote(path.rsplit('/', 1)[-1]).rsplit('.', 1)[0]
    filename = re.sub(r'[_+]+', ' ', filename)
    filename = re.sub(r'[- ]?(?:poster|flyer).*$', '', filename, flags=re.I)
    return clean_text(filename)


def find_title(section, date_block):
    # Event names are sometimes emphasized in the prose instead of marked up
    # as headings (for example, the archived Holiday POPS listing).
    for strong in date_block.find_all('strong'):
        candidate = clean_text(strong)
        if candidate and not DATE_RE.search(candidate) and len(candidate) > 8:
            return candidate

    heading = section.find(['h1', 'h2', 'h3'])
    heading_text = clean_text(heading)
    if heading_text and 'tickets partner' not in heading_text.lower():
        return heading_text

    return image_title(section)


def find_venue(text):
    for venue, (city, country_code) in VENUES.items():
        if venue.lower() in text.lower():
            return venue, city, country_code
    return None


def event_sections(soup):
    layout = soup.select_one('[data-layout-label="Welcome Footer Content"]')
    if not layout:
        return []

    sections = []
    current = []
    for child in layout.children:
        if not getattr(child, 'get', None):
            continue
        classes = child.get('class', [])
        if current and 'horizontalrule-block' in classes:
            fragment = BeautifulSoup(''.join(str(node) for node in current), 'html.parser')
            if DATE_RE.search(clean_text(fragment)):
                sections.append(fragment)
            current = []
        else:
            current.append(child)
    if current:
        fragment = BeautifulSoup(''.join(str(node) for node in current), 'html.parser')
        if DATE_RE.search(clean_text(fragment)):
            sections.append(fragment)
    return sections


def parse_homepage(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for section in event_sections(soup):
        date_block = section.find(string=lambda value: value and DATE_RE.search(value))
        if not date_block:
            continue
        date_container = date_block.parent
        event_date, time_from = parse_datetime(date_container)
        title = find_title(section, date_container)
        venue_data = find_venue(clean_text(section))
        if not event_date or not title or not venue_data:
            continue

        venue, city, country_code = venue_data
        row = date_container.find_parent('div', class_='row')
        link = date_container.find('a', href=True)
        if not link and row:
            link = row.find('a', href=True)
        if not link:
            link = section.find(
                'a',
                href=lambda href: href
                and not any(value in href for value in ('facebook.com', 'instagram.com', 'youtube.com')),
            )
        url = urljoin(SOURCE_URL, link['href']) if link else SOURCE_URL
        description = clean_text(section)
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class GspoComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gspo_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        records = parse_homepage(response.text)
        if not records:
            log_message(
                'No complete concert listings found',
                event='crawler_empty_listing',
                level='warning',
                url=SOURCE_URL,
                record_count=0,
            )
        return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


def main():
    GspoComCrawler().run()


if __name__ == '__main__':
    main()
