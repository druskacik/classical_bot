import html
import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.firelandssymphony.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Firelands Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
}

SLUG_DATE_RE = re.compile(r'^(\d{4})_(\d{2})_(\d{2})(?:-(\d{2}))?(?:-|$)')
DISPLAY_DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*'
    r'([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})',
    re.I,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?', re.I)
CITY_RE = re.compile(r'\b([A-Za-z][A-Za-z .\'’-]*?),\s*OH\b', re.I)
STREET_RE = re.compile(
    r'.*\b(?:Street|St|Road|Rd|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Lane|Ln)\.?\s+', re.I
)


def clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '').replace('\xa0', ' ')).strip()


def slug_dates(slug):
    match = SLUG_DATE_RE.match(slug or '')
    if not match:
        return []
    year, month, first_day, second_day = match.groups()
    values = []
    for day in (first_day, second_day):
        if not day:
            continue
        try:
            values.append(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            return []
    return values


def parse_time(match):
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def clean_city(value):
    return clean_text(STREET_RE.sub('', clean_text(value)))


def is_address(value):
    return bool(re.search(
        r'^\d|\b(?:Street|St|Road|Rd|Drive|Dr|Avenue|Ave|Boulevard|Blvd|Lane|Ln)\.?$',
        clean_text(value),
        re.I,
    ))


def page_lines(soup):
    return [clean_text(item) for item in soup.stripped_strings if clean_text(item)]


def occurrence_details(lines):
    """Return displayed occurrence dates with their nearby times and locations."""
    details = []
    date_indexes = []
    for index, line in enumerate(lines):
        matches = list(DISPLAY_DATE_RE.finditer(line.replace('|', ' ')))
        for match_index, match in enumerate(matches):
            try:
                event_date = datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
            except ValueError:
                continue
            next_start = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(line)
            inline_times = [parse_time(item) for item in TIME_RE.finditer(line[match.end():next_start])]
            date_indexes.append((index, event_date, inline_times))

    for position, (index, event_date, inline_times) in enumerate(date_indexes):
        end = date_indexes[position + 1][0] if position + 1 < len(date_indexes) else min(len(lines), index + 14)
        segment = lines[index + 1:end]
        times = list(inline_times)
        for line in segment[:4]:
            line_matches = list(TIME_RE.finditer(line))
            if 'activit' in line.lower() and 'concert' in line.lower() and line_matches:
                line_matches = [line_matches[-1]]
            for match in line_matches:
                value = parse_time(match)
                if value not in times:
                    times.append(value)

        city = ''
        venue = ''
        for offset, line in enumerate(segment[:10]):
            city_match = CITY_RE.search(line)
            if not city_match:
                continue
            city = clean_city(city_match.group(1))
            prefix = clean_text(line[:city_match.start()].strip(' ,'))
            if prefix and not is_address(prefix):
                venue = prefix
            elif offset:
                for previous in reversed(segment[:offset]):
                    if not is_address(previous) and not TIME_RE.search(previous):
                        venue = clean_text(previous)
                        break
            break
        details.append({'date': event_date, 'times': times, 'venue': venue, 'city': city})
    return details


def location_candidates(soup, lines):
    candidates = []

    # Event detail headings are the most consistent location structure on detail pages.
    for heading in soup.select('h3.elementor-heading-title'):
        venue = clean_text(heading.get_text(' ', strip=True))
        if not venue or venue.lower() in {'follow us', 'event details'}:
            continue
        parent_text = clean_text(heading.parent.parent.parent.get_text(' ', strip=True))
        city_match = CITY_RE.search(parent_text)
        if city_match:
            candidate = (venue, clean_city(city_match.group(1)))
            if candidate not in candidates:
                candidates.append(candidate)

    # Older pages often put the venue and city together in the occurrence summary.
    for index, line in enumerate(lines):
        city_match = CITY_RE.search(line)
        if not city_match:
            continue
        city = clean_city(city_match.group(1))
        venue = clean_text(line[:city_match.start()].strip(' ,'))
        if not venue and index:
            venue = clean_text(lines[index - 1])
        if venue and not TIME_RE.search(venue) and not is_address(venue):
            candidate = (venue, city)
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def description_from_page(soup):
    text = clean_text(soup.get_text(' ', strip=True))
    for marker in ('PRINCIPAL BENEFACTOR', 'Principal Benefactor', 'venue Event Details'):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text or None


def parse_page(page):
    dates = slug_dates(page.get('slug'))
    content = page.get('content', {}).get('rendered', '')
    if not dates or not content:
        return []

    soup = BeautifulSoup(content, 'html.parser')
    lines = page_lines(soup)
    displayed = occurrence_details(lines)
    locations = location_candidates(soup, lines)
    displayed_by_date = {}
    for item in displayed:
        if item['date'] in dates:
            displayed_by_date.setdefault(item['date'], []).append(item)

    raw_title = html.unescape(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text())
    title = clean_text(re.sub(r'^\d{4}_\d{2}_\d{2}(?:-\d{2})?\s*', '', raw_title))
    url = page.get('link', '')
    description = description_from_page(soup)
    records = []

    for index, event_date in enumerate(dates):
        shown = displayed_by_date.get(event_date, [])
        detail = shown[0] if shown else {}
        venue = clean_text(detail.get('venue'))
        city = clean_text(detail.get('city'))
        if (not venue or not city) and locations:
            location = locations[0]
            venue = venue or location[0]
            city = city or location[1]
        if not title or not url or not venue or not city:
            log_message(
                'Skipping event with incomplete required location data',
                event='crawler_record_skipped',
                level='warning',
                url=url,
                event_date=event_date,
            )
            continue

        times = detail.get('times') or [None]
        for time_from in times:
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


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    page_number = 1

    while True:
        response = session.get(
            API_URL,
            params={
                'per_page': 100,
                'page': page_number,
                '_fields': 'slug,link,title,content',
            },
            timeout=45,
        )
        if response.status_code == 400 and page_number > 1:
            break
        response.raise_for_status()
        pages = response.json()
        for page in pages:
            if SLUG_DATE_RE.match(page.get('slug', '')):
                records.extend(parse_page(page))

        total_pages = int(response.headers.get('X-WP-TotalPages', page_number))
        if page_number >= total_pages:
            break
        page_number += 1

    if not records:
        log_message(
            'No parseable dated event pages found',
            event='crawler_empty_listing',
            level='warning',
            url=API_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class FirelandsSymphonyComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='firelandssymphony_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    FirelandsSymphonyComCrawler().run()


if __name__ == '__main__':
    main()
