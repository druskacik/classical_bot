import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://handelandhaydn.org/'
SITEMAP_URL = f'{SOURCE_URL}concerts-sitemap.xml'
SOURCE = 'Handel and Haydn Society'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTH_PATTERN = (
    r'January|February|March|April|May|June|July|August|September|October|'
    r'November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
)
DATE_PATTERN = re.compile(
    rf'(?P<month>{MONTH_PATTERN})\.?\s+(?P<day>\d{{1,2}}),\s*(?P<year>20\d{{2}})'
    rf'(?:\s+at\s+(?P<time>\d{{1,2}}:\d{{2}}\s*[ap]m))?',
    re.IGNORECASE,
)
COMPACT_DATE_PATTERN = re.compile(
    rf'(?P<month>{MONTH_PATTERN})\.?\s+(?P<days>\d{{1,2}}(?:\s*\+\s*\d{{1,2}})+),'
    rf'\s*(?P<year>20\d{{2}})',
    re.IGNORECASE,
)


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_value(soup, label):
    heading = soup.find(
        lambda tag: tag.name in {'h3', 'h4', 'h5', 'h6'}
        and clean_text(tag).lower() == label.lower()
    )
    return heading.find_next_sibling() if heading else None


def parse_date(value):
    parsed = datetime.strptime(value.replace('Sept', 'Sep'), '%B %d, %Y') if len(value.split()[0]) > 3 else datetime.strptime(value.replace('Sept', 'Sep'), '%b %d, %Y')
    return parsed.date().isoformat()


def parse_occurrences(value):
    occurrences = []
    occupied = []
    for match in COMPACT_DATE_PATTERN.finditer(value):
        occupied.append(match.span())
        for day in re.findall(r'\d{1,2}', match.group('days')):
            raw = f"{match.group('month')} {day}, {match.group('year')}"
            try:
                occurrences.append((parse_date(raw), None))
            except ValueError:
                continue

    for match in DATE_PATTERN.finditer(value):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        raw = f"{match.group('month')} {match.group('day')}, {match.group('year')}"
        try:
            event_date = parse_date(raw)
        except ValueError:
            continue
        time_from = None
        if match.group('time'):
            time_from = datetime.strptime(
                re.sub(r'\s+', '', match.group('time')).upper(), '%I:%M%p'
            ).strftime('%H:%M')
        occurrences.append((event_date, time_from))
    return list(dict.fromkeys(occurrences))


def venue_for_occurrence(default_venue, date_text, match_text):
    following = date_text[date_text.find(match_text) + len(match_text):]
    at_match = re.match(r'\s+at\s+([^\n|]+)', following, re.IGNORECASE)
    if at_match:
        candidate = re.sub(r'\s*\([^)]*\)\s*$', '', at_match.group(1)).strip(' .')
        if candidate and not re.match(r'\d{1,2}:\d{2}', candidate):
            return candidate
    if ' and ' not in default_venue and ' + ' not in default_venue:
        return default_venue
    return ''


def city_for_venue(venue, url):
    value = venue.lower()
    explicit = re.search(r'\b(cambridge|arlington|brookline|lexington|marblehead|boston|roxbury)\b', value)
    if explicit:
        city = explicit.group(1).title()
        return 'Boston' if city == 'Roxbury' else city
    if 'marblehead' in url:
        return 'Marblehead'
    if any(name in value for name in ('symphony hall', 'jordan hall', 'williams hall',
                                      'trinity church', 'klarman hall', 'old south church')):
        return 'Boston'
    if 'sanders theatre' in value or 'first church in cambridge' in value:
        return 'Cambridge'
    return ''


def event_description(soup):
    parts = []
    overview = soup.select_one('.concert-overview-left-col')
    if overview:
        text = clean_text(overview)
        if text:
            parts.append(text)
    dates_heading = soup.find(
        lambda tag: tag.name in {'h3', 'h4', 'h5', 'h6'}
        and clean_text(tag).lower() == 'dates'
    )
    if dates_heading:
        details = clean_text(dates_heading.parent)
        if details:
            parts.append(details)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1') or soup.select_one('h1'))
    venue = clean_text(labelled_value(soup, 'Location'))
    dates = clean_text(labelled_value(soup, 'Dates'))
    if not title or not venue or not dates:
        return []
    if re.search(r'\b(streaming|online stream|broadcasting)\b', venue, re.IGNORECASE):
        return []

    description = event_description(soup)
    records = []
    for event_date, time_from in parse_occurrences(dates):
        date_match = next(
            (match.group(0) for match in DATE_PATTERN.finditer(dates)
             if event_date == parse_date(
                 f"{match.group('month')} {match.group('day')}, {match.group('year')}"
             )),
            '',
        )
        raw_venue = venue_for_occurrence(venue, dates, date_match) if date_match else venue
        city = city_for_venue(raw_venue, url)
        event_venue = re.sub(r'\s*\([^)]*\)?\s*$', '', raw_venue).strip()
        if not event_venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': event_venue,
            'city': city,
            'description': description,
        })
    return records


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class HandelAndHaydnOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='handelandhaydn_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, 'xml')
        urls = [
            clean_text(location)
            for location in sitemap.find_all('loc')
            if '/concerts/' in clean_text(location)
            and '%season%' not in clean_text(location)
        ]

        records = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    records.extend(future.result())
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Handel and Haydn concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    HandelAndHaydnOrgCrawler().run()


if __name__ == '__main__':
    main()
