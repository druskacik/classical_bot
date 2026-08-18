import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.echorchestra.com/'
SOURCE = 'ECHO Chamber Orchestra'
SEASON_PATHS = (
    '2026-2027-season',
    '2025-26-season',
    '2024-25season',
    'copy-of-2022-23-season',
    '2022-2023season',
    '2021-2022season',
    '2020-2021season',
    '2019-2020season',
    '2018-2019season',
    '2017-2018season',
    '2016-2017season',
    '2015-2016season',
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}
MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|November|December'
)
DATE_RE = re.compile(
    rf'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    rf'(?P<month>{MONTHS})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s*'
    rf'(?P<year>20\s*\d{{2}})?(?:\s*(?:at|,)?\s*'
    rf'(?P<time>\d{{1,2}}(?::\s*\d{{1,2}})?\s*(?:am|pm)))?',
    re.IGNORECASE,
)
CITY_RE = re.compile(r'\b(San Anselmo|San Rafael|Oakland|Berkeley)\b', re.IGNORECASE)
LOCATION_TAIL_RE = re.compile(
    r'^.*\b(?:San Anselmo|San Rafael|Oakland|Berkeley)\b(?:,?\s*CA)?(?:\s*\d{5})?\s*',
    re.IGNORECASE | re.DOTALL,
)
VENUES = {
    'san anselmo': 'First Presbyterian Church',
    'san rafael': 'First Presbyterian Church',
    'oakland': 'St. Paul Lutheran Church',
    'berkeley': 'Freight and Salvage Coffee House',
}


def clean_text(value):
    text = re.sub(r'[\u200b\ufeff]', '', value or '')
    text = text.replace('\xa0', ' ').replace('\u2028', ' ')
    text = re.sub(rf'\b({MONTHS})\s*\|\s*(\d{{1,2}}|[a-z])', r'\1 \2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(Jun|Septemb|Februar)\s*\|\s*([ey])\b', r'\1\2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(20\d?)\s*\|\s*(\d{1,2})\b', r'\1\2', text)
    text = re.sub(r'\b(\d{1,2})t\s*\|\s*h\b', r'\1th', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(\d{1,2}(?:st|nd|rd|th)?),?\s*\|\s*(20\d{2})\b', r'\1, \2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(20\d{2}),?\s*\|\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', r'\1, \2', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d:\d)\s*\|\s*(\d\s*(?:am|pm))\b', r'\1\2', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(20)\s+(\d{2})\b', r'\1\2', text)
    text = re.sub(r'\b(Jun|Septemb|Februar)\s+([ey])\b', r'\1\2', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d:\d)\s+(\d\s*(?:am|pm))\b', r'\1\2', text, flags=re.IGNORECASE)
    text = text.replace("New Year's Opene | r", "New Year's Opener")
    text = re.sub(r'\bp\s*\|\s*romenade\b', 'Promenade', text, flags=re.IGNORECASE)
    text = re.sub(r'\bpromenade\b', 'Promenade', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip(' |')


def parse_date(match, season_start):
    year_text = match.group('year')
    if year_text:
        year = int(re.sub(r'\s+', '', year_text))
    else:
        month = datetime.strptime(match.group('month')[:3], '%b').month
        year = season_start if month >= 7 else season_start + 1
    raw = f"{match.group('month')} {match.group('day')} {year}"
    return datetime.strptime(raw, '%B %d %Y').date().isoformat()


def parse_time(value):
    if not value:
        return None
    normalized = re.sub(r'\s+', '', value).upper()
    if ':' not in normalized:
        normalized = re.sub(r'(?i)(AM|PM)$', r':00\1', normalized)
    return datetime.strptime(normalized, '%I:%M%p').strftime('%H:%M')


def event_location(text):
    cities = CITY_RE.findall(text)
    city = cities[-1].title() if cities else 'San Anselmo'
    return VENUES[city.casefold()], city


def title_and_description(prefix):
    prefix = LOCATION_TAIL_RE.sub('', prefix)
    parts = [clean_text(part) for part in prefix.split('|')]
    parts = [part for part in parts if part and not re.match(r'^(?:Concert )?Season\b', part, re.I)]
    if not parts:
        return None, None
    title = parts[0]
    description = '\n'.join(parts[1:]) or None
    return title, description


def records_from_page(html, page_url, season_start):
    soup = BeautifulSoup(html, 'html.parser')
    blocks = []
    for element in soup.select('main div.wixui-rich-text'):
        text = clean_text(' | '.join(element.stripped_strings))
        if text:
            blocks.append(text)

    records = []
    page_text = clean_text((soup.find('main') or soup).get_text(' ', strip=True))
    for block_index, block in enumerate(blocks):
        matches = list(DATE_RE.finditer(block))
        if not matches:
            continue
        previous_end = 0
        for match_index, match in enumerate(matches):
            # Undated online webinar sessions are not concrete concert records.
            if not match.group('year') and season_start != 2026:
                previous_end = match.end()
                continue
            prefix = block[previous_end:match.start()]
            title, description = title_and_description(prefix)
            previous_end = match.end()
            if not title and len(matches) == 1:
                following = []
                for candidate in blocks[block_index + 1:]:
                    if DATE_RE.search(candidate) or candidate.lower().startswith('all performances'):
                        break
                    following.append(candidate)
                if following:
                    title_parts = [clean_text(part) for part in following[0].split('|') if clean_text(part)]
                    if title_parts:
                        title = title_parts[0]
                        description_parts = title_parts[1:] + following[1:]
                        description = '\n'.join(description_parts) or None
            if not title:
                continue

            next_start = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(block)
            location_context = block[match.end():next_start]
            venue, city = event_location(location_context or page_text)
            records.append({
                'title': title,
                'date': parse_date(match, season_start),
                'url': page_url,
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
    records = []
    for path in SEASON_PATHS:
        page_url = SOURCE_URL + path
        try:
            response = session.get(page_url, headers=HEADERS, timeout=60)
            response.raise_for_status()
            season_match = re.search(r'(20\d{2})', path)
            records.extend(records_from_page(response.text, page_url, int(season_match.group(1))))
        except requests.RequestException as error:
            log_message(
                'ECHO season page request failed',
                event='crawler_page_failed',
                level='warning',
                url=page_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    unique = {
        (record['title'], record['date'], record['time_from'], record['venue']): record
        for record in records
    }
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No ECHO concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return result


class EchorchestraComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='echorchestra_com',
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
    EchorchestraComCrawler().run()


if __name__ == '__main__':
    main()
