import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.boulderphil.org/'
SEASON_URL = urljoin(SOURCE_URL, '2627-season')
SOURCE = 'Boulder Philharmonic Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*\|\s*'
    r'((?:January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+\d{1,2},\s+20\d{2})\s*'
    r'(?:at\s*|\|\s*(?:(.+?)\s+at\s+)?)'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)',
    re.I,
)


def clean_text(element):
    if not element:
        return ''
    text = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', text).strip()


def parse_time(value):
    normalized = re.sub(r'\s*([AP]M)$', r' \1', value.strip().upper())
    return datetime.strptime(
        normalized,
        '%I:%M %p' if ':' in value else '%I %p',
    ).strftime('%H:%M')


def event_url(section):
    links = [
        urljoin(SEASON_URL, anchor.get('href'))
        for anchor in section.select('a[href]')
        if anchor.get('href') and not anchor.get('href').startswith(('#', 'mailto:'))
    ]
    for anchor in section.select('a[href]'):
        if re.search(r'(?:details|tickets)', clean_text(anchor), re.I):
            return urljoin(SEASON_URL, anchor['href'])
    return links[0] if links else SEASON_URL


def title_for(section):
    headings = [clean_text(heading) for heading in section.select('h4')]
    return next(
        (heading for heading in headings if not re.fullmatch(r'[A-Z]{3,4}\s+\d{1,2}(?:\s*&\s*\d{1,2})?', heading)),
        '',
    )


def location_for(title, info, last_match):
    tail = info[last_match.end():]
    tail = re.sub(r'^\s*(?:\([^)]*\)\s*)?(?:\|\s*FREE\s*)?', '', tail, flags=re.I)
    tail = re.split(r'\s+with\s+', tail, maxsplit=1, flags=re.I)[0].strip(' |')

    location = re.match(r'(.+?)\s*\|\s*([^|]+?),\s*CO\b', tail)
    if location:
        return location.group(1).strip(), location.group(2).strip()

    city = re.match(r'([^|]+?),\s*CO\b', tail)
    if not city:
        return '', ''
    city_name = city.group(1).strip()
    if 'Nutcracker' in title:
        return 'Macky Auditorium', city_name
    if 'Levitt Pavilion' in title:
        return 'Levitt Pavilion', city_name
    return '', city_name


def parse_section(section):
    title = title_for(section)
    paragraphs = section.select('p')
    info_element = next((p for p in paragraphs if DATE_RE.search(clean_text(p))), None)
    if not title or not info_element:
        return []

    info = clean_text(info_element)
    matches = list(DATE_RE.finditer(info))
    venue, city = location_for(title, info, matches[-1])
    if matches[-1].group(2):
        venue = matches[-1].group(2).strip()

    # One community listing places its venue before the time rather than after it.
    if not venue:
        unusual = re.search(
            r'20\d{2}\s*\|\s*(.+?)\s+at\s+\d{1,2}(?::\d{2})?\s*[AP]M\s+([^|]+?),\s*CO\b',
            info,
            re.I,
        )
        if unusual:
            venue, city = unusual.group(1).strip(), unusual.group(2).strip()

    if not venue or not city:
        log_message(
            'Skipped Boulder Phil event with incomplete location',
            event='crawler_item_skipped',
            level='warning',
            url=event_url(section),
            error_type='IncompleteEventData',
            error_message='Required venue or city is missing',
        )
        return []

    descriptions = [clean_text(p) for p in paragraphs if p is not info_element]
    description = max(descriptions, key=len, default='') or None
    url = event_url(section)
    records = []
    for match in matches:
        try:
            event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
            time_from = parse_time(match.group(3))
        except ValueError:
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


class BoulderPhilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='boulderphil_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SEASON_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for section in soup.select('main section[data-section-id]'):
            records.extend(parse_section(section))
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    BoulderPhilOrgCrawler().run()


if __name__ == '__main__':
    main()
