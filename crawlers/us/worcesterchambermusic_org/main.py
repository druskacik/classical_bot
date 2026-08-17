import html
import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://worcesterchambermusic.org/'
SOURCE = 'Worcester Chamber Music Society'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
MONTHS = {
    name: number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July',
         'August', 'September', 'October', 'November', 'December'), 1
    )
}
DATE_RE = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2})(?:st|nd|rd|th)?', re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?', re.IGNORECASE)
CITY_RE = re.compile(r'\b(Worcester|Harvard|Princeton|Fitchburg|Clinton)\b', re.IGNORECASE)
VENUE_WORDS = re.compile(
    r'\b(?:church|hall|museum|university|center|centre|restaurant|library|auditorium|theatre|theater|antiquarian\s+society)\b',
    re.IGNORECASE,
)


def clean(value):
    value = html.unescape(value or '').replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', value).strip(' \n\t–-')


def content_lines(rendered):
    rendered = re.sub(r'<br\s*/?>', '\n', rendered, flags=re.IGNORECASE)
    rendered = re.sub(r'\[/?[^\]]+\]', '\n', rendered)
    text = BeautifulSoup(rendered, 'html.parser').get_text('\n')
    return [clean(line) for line in text.splitlines() if clean(line)]


def season_years(title):
    match = re.search(r'\b(20\d{2})(?:-(?:20)?(\d{2,4}))?', title)
    if not match:
        return None
    start = int(match.group(1))
    if not match.group(2):
        return start, start
    end_text = match.group(2)
    end = int(end_text) if len(end_text) == 4 else (start // 100) * 100 + int(end_text)
    return start, end


def event_date(month, day, years):
    month_number = MONTHS[month.capitalize()]
    year = years[0] if month_number >= 7 else years[1]
    try:
        return date(year, month_number, int(day)).isoformat()
    except ValueError:
        return None


def parse_time(value, concert_time=False):
    if concert_time:
        concert_range = re.search(
            r'(\d{1,2}):([0-5]\d)\s*[-–]\s*\d{1,2}:[0-5]\d\s*([ap])\.?m\.?\s*concert',
            value, re.IGNORECASE,
        )
        if concert_range:
            hour = int(concert_range.group(1)) % 12
            if concert_range.group(3).lower() == 'p':
                hour += 12
            return f'{hour:02d}:{concert_range.group(2)}'
    matches = list(TIME_RE.finditer(value))
    if not matches:
        return None
    match = matches[-2] if concert_time and 'concert' in value.lower() and len(matches) > 1 else matches[0]
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def city_from(value):
    match = CITY_RE.search(value)
    return match.group(1).title() if match else None


def venue_after(lines, index):
    line = lines[index]
    inline = re.search(r'\|\s*(.+?)\s*@', line)
    if inline and VENUE_WORDS.search(inline.group(1)):
        venue = clean(inline.group(1))
        # The Hanover Theatre is in Worcester; the season page identifies the city.
        return venue, city_from(venue) or ('Worcester' if 'Hanover' in venue else None)
    candidates = lines[index + 1:index + 4]
    for offset, line in enumerate(candidates):
        if DATE_RE.search(line):
            break
        if VENUE_WORDS.search(line):
            city = city_from(line)
            if city:
                return line, city
            for following in candidates[offset + 1:]:
                following_city = city_from(following)
                if following_city:
                    return line, following_city
    return None, None


def record(title, event_date_value, url, time_from, venue, city, description):
    return {
        'title': clean(title),
        'date': event_date_value,
        'url': url,
        'time_from': time_from,
        'venue': clean(venue),
        'city': city,
        'country_code': 'US',
        'description': clean(description) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_detail(page, years):
    title = clean(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text(' '))
    lines = content_lines(page['content']['rendered'])
    description = ' '.join(lines)
    records = []
    for index, line in enumerate(lines):
        match = DATE_RE.search(line)
        if not match:
            continue
        venue, city = venue_after(lines, index)
        # The Café page states its single location once, before both occurrences.
        if not venue and title.lower() == 'café concerts':
            venue, city = 'Nuovo Restaurant', 'Worcester'
        if not venue or not city:
            continue
        date_value = event_date(match.group(1), match.group(2), years)
        if date_value:
            records.append(record(
                title, date_value, page['link'], parse_time(line, concert_time=True),
                venue, city, description,
            ))
    return records


def parse_chamberfest(page, years):
    lines = content_lines(page['content']['rendered'])
    joined = ' '.join(lines)
    venue = None
    for index, line in enumerate(lines[:10]):
        if VENUE_WORDS.search(line) and any(
            city_from(candidate) == 'Worcester' for candidate in lines[index:index + 2]
        ):
            venue = clean(re.sub(r'^Venue:\s*', '', line, flags=re.IGNORECASE))
            venue = re.sub(r',?\s*\d+\s+[^,]+,?\s*Worcester.*$', '', venue, flags=re.IGNORECASE)
            venue = re.sub(r',?\s*Worcester.*$', '', venue, flags=re.IGNORECASE)
            break
    if not venue:
        return []
    date_lines = [(i, line, DATE_RE.search(line)) for i, line in enumerate(lines) if DATE_RE.search(line)]
    # The 2023 archive prints "July 8, 13, and 15" once above three programmes.
    if len(date_lines) == 1:
        combined = re.search(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2}),\s*(\d{1,2}),\s*(?:and\s+)?(\d{1,2})', joined, re.IGNORECASE,
        )
        if combined:
            base_index = date_lines[0][0]
            date_lines = [
                (base_index, f'{combined.group(1)} {day}', DATE_RE.search(f'{combined.group(1)} {day}'))
                for day in combined.groups()[1:]
            ]
    records = []
    for number, (index, line, match) in enumerate(date_lines, 1):
        date_value = event_date(match.group(1), match.group(2), years)
        if not date_value:
            continue
        end = date_lines[number][0] if number < len(date_lines) and date_lines[number][0] > index else len(lines)
        description = ' '.join(lines[index:end])
        records.append(record(
            f'ChamberFest Summer Concert {number}', date_value, page['link'],
            parse_time(line) or parse_time(joined), venue, 'Worcester', description,
        ))
    return records


class WorcesterChamberMusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='worcesterchambermusic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            pages = []
            page_number = 1
            while True:
                response = session.get(
                    API_URL, params={'per_page': 100, 'page': page_number}, timeout=60
                )
                if response.status_code == 400 and page_number > 1:
                    break
                response.raise_for_status()
                batch = response.json()
                pages.extend(batch)
                if len(batch) < 100:
                    break
                page_number += 1
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Worcester Chamber Music Society pages',
                event='crawler_fetch_failed', level='error', url=API_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        by_slug = {page['slug']: page for page in pages}
        records = []
        detail_slugs = set()
        for page in pages:
            title = clean(BeautifulSoup(page['title']['rendered'], 'html.parser').get_text(' '))
            years = season_years(title)
            if not years:
                continue
            if re.search(r'concert schedule|20\d{2}-20\d{2} concerts', title, re.IGNORECASE):
                soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
                for link in soup.find_all('a', href=True):
                    href = link['href']
                    if urlparse(href).netloc != urlparse(SOURCE_URL).netloc:
                        continue
                    slug = urlparse(href).path.strip('/').split('/')[-1]
                    if slug in by_slug and slug != page['slug']:
                        detail_slugs.add((slug, years))
            elif re.search(r'ChamberFest Summer Concerts', title, re.IGNORECASE):
                records.extend(parse_chamberfest(page, years))

        # Detail pages linked from a season index include the complete programme.
        for slug, years in detail_slugs:
            records.extend(parse_detail(by_slug[slug], years))

        valid = [item for item in records if all(
            item.get(field) for field in
            ('title', 'date', 'url', 'venue', 'city', 'country_code', 'source_url', 'source')
        )]
        log_message(
            'Scraped Worcester Chamber Music Society concerts',
            event='crawler_scrape_completed', url=SOURCE_URL, record_count=len(valid),
        )
        return valid


def main():
    return WorcesterChamberMusicOrgCrawler().run()


if __name__ == '__main__':
    main()
