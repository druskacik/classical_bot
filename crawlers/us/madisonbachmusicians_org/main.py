import re
from datetime import datetime
from html import unescape
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://madisonbachmusicians.org/'
SOURCE = 'Madison Bach Musicians'
PAGES_API = f'{SOURCE_URL}wp-json/wp/v2/pages'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_RE = re.compile(
    rf'\b({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(20\d{{2}})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?', re.IGNORECASE)

# Event pages use purpose-built templates. The season page links ensure that a
# newly named current template is also discovered; these tokens retain archived
# concert and recital pages after they leave the season navigation.
EVENT_TEMPLATE_TOKENS = ('concert', 'recital', 'requiem', 'cantatas', 'baroque', 'princely')
EXCLUDED_TEMPLATE_TOKENS = ('workshop', 'scholarship', 'organ-donation')

VENUE_CITIES = {
    'First Congregational Church': 'Madison',
    'First Unitarian Society, Landmark Auditorium': 'Madison',
    'Holy Wisdom Monastery': 'Middleton',
    'UW Hamel Music Center-Mead Witter Foundation Hall': 'Madison',
    'University of Wisconsin-Madison Hamel Music Center': 'Madison',
    'Four Winds Farm': 'Fitchburg',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def page_content(heading):
    if not heading:
        return []

    lines = []
    for element in heading.next_elements:
        if isinstance(element, Tag) and element.name in {'h1', 'h2', 'h3', 'h4'}:
            if clean_text(element.get_text(' ', strip=True)).lower() == 'other events':
                break
        if isinstance(element, NavigableString):
            text = clean_text(element)
            if text and (not lines or lines[-1] != text):
                lines.append(text)
    return lines


def parse_date(match):
    try:
        return datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None


def parse_concert_time(line):
    matches = list(TIME_RE.finditer(line))
    if not matches:
        return None
    concert_matches = [match for match in matches if 'concert' in line[match.end():].lower()]
    match = concert_matches[-1] if concert_matches else matches[-1]
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'p' else 0)
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def venue_and_city(lines, date_index, date_indexes):
    for index in range(date_index - 1, max(-1, date_index - 15), -1):
        candidate = clean_text(lines[index])
        if DATE_RE.search(candidate):
            continue
        if candidate in VENUE_CITIES:
            return candidate, VENUE_CITIES[candidate]
        if ',' in candidate:
            venue, city = [part.strip() for part in candidate.rsplit(',', 1)]
            if venue and city in {'Madison', 'Middleton', 'Fitchburg'}:
                return venue, city

    # Some pages put one shared venue after both performance dates.
    last_date = max(date_indexes)
    for candidate in lines[last_date + 1:last_date + 10]:
        candidate = clean_text(candidate)
        if candidate in VENUE_CITIES:
            return candidate, VENUE_CITIES[candidate]
        if candidate.lower() in {'purchase tickets', 'sold out', 'parking information'}:
            continue
        if ',' in candidate:
            venue, city = [part.strip() for part in candidate.rsplit(',', 1)]
            if venue and city in {'Madison', 'Middleton', 'Fitchburg'}:
                return venue, city
    return None, None


def parse_event_page(html, url, fallback_title=''):
    soup = BeautifulSoup(html, 'html.parser')
    fallback_title = clean_text(unescape(fallback_title))
    headings = soup.find_all(['h1', 'h2', 'h3'])
    heading = next(
        (item for item in headings if clean_text(item.get_text(' ', strip=True)) == fallback_title),
        None,
    )
    if not heading:
        heading = next(
            (item for item in headings if clean_text(item.get_text(' ', strip=True)) != SOURCE),
            None,
        )
    title = clean_text(heading.get_text(' ', strip=True) if heading else fallback_title)
    lines = page_content(heading)
    normalized_lines = []
    for line in lines:
        if (
            normalized_lines
            and re.search(rf'\b(?:{MONTHS})\s+\d{{1,2}}$', normalized_lines[-1], re.I)
            and re.match(r'^,?\s*20\d{2}\b', line)
        ):
            normalized_lines[-1] += line
        else:
            normalized_lines.append(line)
    lines = normalized_lines
    occurrences = [
        (index, match, line)
        for index, line in enumerate(lines)
        for match in DATE_RE.finditer(line)
    ]
    date_indexes = [index for index, _, _ in occurrences]
    if not title or not occurrences:
        return []

    stop_words = {'purchase tickets', 'sold out', 'parking information', 'about this program'}
    description_lines = []
    description_started = False
    for line in lines:
        if line.lower() == 'about this program':
            description_started = True
            continue
        if description_started and line.lower() not in stop_words:
            description_lines.append(line)
    description = '\n'.join(description_lines) or None

    records = []
    for occurrence_number, (date_index, match, line) in enumerate(occurrences):
        event_date = parse_date(match)
        venue, city = venue_and_city(lines, date_index, date_indexes)
        if not event_date or not venue or not city:
            continue
        next_occurrence = occurrences[occurrence_number + 1] if occurrence_number + 1 < len(occurrences) else None
        if next_occurrence and next_occurrence[0] == date_index:
            time_text = line[match.end():next_occurrence[1].start()]
        else:
            next_date_index = next_occurrence[0] if next_occurrence else date_index + 10
            time_text = ' '.join(lines[date_index:min(next_date_index, date_index + 10)])
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_concert_time(time_text),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def is_event_template(template):
    template = clean_text(template).lower()
    return (
        template
        and not any(token in template for token in EXCLUDED_TEMPLATE_TOKENS)
        and any(token in template for token in EVENT_TEMPLATE_TOKENS)
    )


def discover_pages(session):
    pages = []
    page_number = 1
    while True:
        response = session.get(
            PAGES_API,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,template',
            },
            timeout=45,
        )
        response.raise_for_status()
        batch = response.json()
        pages.extend(batch)
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page_number >= total_pages:
            break
        page_number += 1

    season = next((page for page in pages if page.get('slug') == 'season'), None)
    season_links = set()
    if season:
        response = session.get(season['link'], timeout=45)
        response.raise_for_status()
        for link in BeautifulSoup(response.text, 'html.parser').select('a[href]'):
            href = link.get('href', '').split('#', 1)[0].rstrip('/') + '/'
            if urlparse(href).netloc == 'madisonbachmusicians.org':
                season_links.add(href)

    return [
        page for page in pages
        if is_event_template(page.get('template'))
        or page.get('link', '').rstrip('/') + '/' in season_links
    ]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for page in discover_pages(session):
        url = page['link']
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            records.extend(parse_event_page(response.text, url, page['title']['rendered']))
        except requests.RequestException as error:
            log_message(
                'Concert page request failed',
                event='crawler_page_request_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['venue']))
    if not result:
        log_message(
            'No concrete concert performances found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class MadisonBachMusiciansOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='madisonbachmusicians_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    MadisonBachMusiciansOrgCrawler().run()


if __name__ == '__main__':
    main()
