import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.chambermusicpittsburgh.org/'
SOURCE = 'Chamber Music Pittsburgh'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-page-1.xml'
PITTSBURGH_PERFORMS_URL = f'{SOURCE_URL}pittsburgh-performs/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4}),\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*([AP]M)',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None
    month, day, year, hour, minute, meridiem = match.groups()
    try:
        parsed = datetime.strptime(
            f'{month} {day} {year} {hour}:{minute or "00"} {meridiem}',
            '%B %d %Y %I:%M %p',
        )
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def city_from_location(location):
    if re.search(r'\bMillvale\b', location, re.IGNORECASE):
        return 'Millvale'
    return 'Pittsburgh'


def venue_from_location(location):
    location = clean_text(location)
    if '|' in location:
        location = clean_text(location.split('|', 1)[1])
    venue = clean_text(location.split(',', 1)[0])
    return venue


def description_from_soup(soup):
    main = soup.select_one('#main-content') or soup
    parts = []
    for node in main.select('p, li'):
        text = clean_text(node.get_text(' ', strip=True))
        if text and text not in parts and not re.fullmatch(
            r'(?:subscribe|buy|purchase|get) tickets?', text, re.IGNORECASE
        ):
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('#main-content') or soup
    headings = main.select('h1, h2, h3, h4')
    date_heading = next(
        (heading for heading in headings if parse_date_time(heading.get_text(' ', strip=True))),
        None,
    )
    if date_heading is None:
        return None

    parsed = parse_date_time(date_heading.get_text(' ', strip=True))
    preceding = headings[:headings.index(date_heading)]
    title = clean_text(preceding[-1].get_text(' ', strip=True)) if preceding else ''
    if not title or title.lower() in {'mainstage live', 'just summer series 2024'}:
        return None

    location = ''
    date_module = date_heading.find_parent(class_='et_pb_module') or date_heading.parent
    location_node = date_module.find('p')
    if location_node:
        location = clean_text(location_node.get_text(' ', strip=True))
    for module in date_module.find_all_next(class_='et_pb_module', limit=4):
        if location:
            break
        candidate = clean_text(module.get_text(' ', strip=True))
        if candidate and not parse_date_time(candidate) and (
            ',' in candidate or 'Pittsburgh' in candidate or 'Millvale' in candidate
        ):
            location = candidate
            break
    venue = venue_from_location(location)
    if not venue:
        return None

    return {
        'title': title,
        'date': parsed[0],
        'url': url,
        'time_from': parsed[1],
        'venue': venue,
        'city': city_from_location(location),
        'country_code': 'US',
        'description': description_from_soup(soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_pittsburgh_performs(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('#main-content') or soup
    event_headings = []
    for heading in main.select('h3'):
        next_heading = heading.find_next(['h2', 'h3'])
        section_text = ' '.join(
            clean_text(node.get_text(' ', strip=True))
            for node in heading.find_all_next('p', limit=8)
            if next_heading is None or node.sourceline is None
            or next_heading.sourceline is None or node.sourceline < next_heading.sourceline
        )
        if parse_date_time(section_text):
            event_headings.append(heading)
    records = []
    for heading in event_headings:
        title = clean_text(heading.get_text(' ', strip=True))
        section_parts = []
        for node in heading.find_all_next(['h2', 'h3', 'p']):
            if node.name in {'h2', 'h3'}:
                break
            text = clean_text(node.get_text(' ', strip=True))
            if text and text not in section_parts:
                section_parts.append(text)
        description = '\n\n'.join(section_parts) or None
        for part in section_parts:
            parsed = parse_date_time(part)
            if not parsed or '|' not in part:
                continue
            location = clean_text(part.split('|', 1)[1])
            venue = venue_from_location(location)
            if not venue:
                continue
            records.append({
                'title': title,
                'date': parsed[0],
                'url': PITTSBURGH_PERFORMS_URL,
                'time_from': parsed[1],
                'venue': venue,
                'city': city_from_location(location),
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


def sitemap_urls(html):
    soup = BeautifulSoup(html, 'xml')
    return [clean_text(node.get_text()) for node in soup.select('url > loc')]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    response = session.get(SITEMAP_URL, timeout=45)
    response.raise_for_status()

    records = []
    for url in sitemap_urls(response.text):
        if url == PITTSBURGH_PERFORMS_URL:
            page = session.get(url, timeout=45)
            page.raise_for_status()
            records.extend(parse_pittsburgh_performs(page.text))
            continue
        page = session.get(url, timeout=45)
        page.raise_for_status()
        record = parse_detail_page(page.text, url)
        if record:
            records.append(record)

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'], item['title']))
    if not result:
        log_message(
            'No concert records found',
            event='crawler_empty_listing',
            level='warning',
            url=SITEMAP_URL,
            record_count=0,
        )
    return result


class ChamberMusicPittsburghOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='chambermusicpittsburgh_org',
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
    ChamberMusicPittsburghOrgCrawler().run()


if __name__ == '__main__':
    main()
