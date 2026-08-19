import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.okcphil.org/'
SOURCE = 'Oklahoma City Philharmonic'
STARTING_SEASON_URL = urljoin(SOURCE_URL, 'events-tickets/26-27-season/')
DEFAULT_CITY = 'Oklahoma City'
DEFAULT_VENUE = 'Civic Center Music Hall'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\.?,?\s+)?'
    r'(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<period>[AP])\.?M\.?', re.I)
RANGE_RE = re.compile(
    r'(?P<month_one>[A-Za-z]{3,9})\s+(?P<day_one>\d{1,2})\s*[\u2013\u2014-]\s*'
    r'(?:(?P<month_two>[A-Za-z]{3,9})\s+)?(?P<day_two>\d{1,2}),\s*(?P<year>\d{4})',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_calendar_date(month, day, year):
    for pattern in ('%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(f'{month} {day} {year}', pattern).date()
        except ValueError:
            pass
    return None


def parse_listing_dates(value):
    text = clean_text(value)
    range_match = RANGE_RE.search(text)
    if range_match:
        values = range_match.groupdict()
        start = parse_calendar_date(values['month_one'], values['day_one'], values['year'])
        end = parse_calendar_date(
            values['month_two'] or values['month_one'], values['day_two'], values['year']
        )
        if not start or not end or end < start:
            return []
        # Concert series on this site span at most a few consecutive dates. Longer
        # ranges represent exhibitions or fundraising campaigns, not performances.
        if (end - start).days > 3:
            return []
        return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]

    match = DATE_RE.search(text)
    if not match:
        return []
    parsed = parse_calendar_date(match['month'], match['day'], match['year'])
    return [parsed.isoformat()] if parsed else []


def parse_time(value):
    match = TIME_RE.search(clean_text(value))
    if not match:
        return None
    hour = int(match['hour']) % 12
    if match['period'].upper() == 'P':
        hour += 12
    return f"{hour:02d}:{int(match['minute'] or 0):02d}"


def request_html(url, session=None):
    requester = session or requests
    response = requester.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response.text


def season_cards(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    cards = []
    for node in soup.select('.concert-info-card'):
        category_node = node.select_one('h5')
        date_node = node.select_one('h4')
        title_node = node.select_one('h3')
        link = node.find('a', href=True)
        if not date_node or not title_node or not link:
            continue
        title = clean_text(title_node.get_text(' ', strip=True))
        url = urljoin(page_url, link['href'])
        dates = parse_listing_dates(date_node.get_text(' ', strip=True))
        category = clean_text(category_node.get_text(' ', strip=True)).upper() if category_node else ''
        if not title or not dates or urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
            continue
        cards.append({'title': title, 'dates': dates, 'url': url, 'category': category})

    previous = next(
        (
            urljoin(page_url, link['href'])
            for link in soup.find_all('a', href=True)
            if 'previous season' in clean_text(link.get_text(' ', strip=True)).lower()
        ),
        None,
    )
    return cards, previous


def detail_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article')
    heading = article.select_one('h1') if article else None
    if not article or not heading:
        return None

    header = heading.find_parent(class_='et_pb_text_inner') or heading.parent
    header_lines = [clean_text(line) for line in header.get_text('\n', strip=True).splitlines()]
    header_lines = [line for line in header_lines if line]
    date_times = {}
    venue = ''
    venue_candidates = []
    last_date_index = 0
    for index, line in enumerate(header_lines[1:], start=1):
        date_match = DATE_RE.search(line)
        if not date_match:
            continue
        last_date_index = index
        parsed = parse_calendar_date(date_match['month'], date_match['day'], date_match['year'])
        if parsed:
            date_times[parsed.isoformat()] = parse_time(line)
        remainder = clean_text(line[(TIME_RE.search(line).end() if TIME_RE.search(line) else date_match.end()):])
        remainder = re.sub(r'^[\s\u2013\u2014|,-]+', '', remainder)
        if remainder and not DATE_RE.search(remainder) and not TIME_RE.search(remainder):
            venue_candidates.append(remainder)

    venue_candidates.extend(header_lines[last_date_index + 1:])
    for candidate in reversed(venue_candidates):
        candidate = clean_text(candidate)
        candidate = re.sub(r'^Location:\s*', '', candidate, flags=re.I)
        candidate = re.sub(r'\s*[\u2013\u2014-]\s*Free Concert.*$', '', candidate, flags=re.I)
        candidate = re.sub(r'\s+\d{2,}\s+.*$', '', candidate)
        if (
            candidate
            and not DATE_RE.search(candidate)
            and not TIME_RE.search(candidate)
            and not candidate.startswith(('(', ')'))
            and candidate.lower() != 'cancelled'
            and not candidate.lower().startswith('free concert')
        ):
            venue = candidate
            break

    article_text = clean_text(article.get_text('\n', strip=True))
    article_text = re.split(r'\n(?:GETTING HERE|WHILE HERE|FAQS)\b', article_text, maxsplit=1, flags=re.I)[0]
    description_lines = []
    header_values = set(header_lines)
    for line in article_text.splitlines():
        line = clean_text(line)
        if not line or line in header_values or line.upper() in {
            'BUY TICKETS', 'CREATE-YOUR-OWN SEASON!', 'CONCERT DETAILS',
        }:
            continue
        if line not in description_lines:
            description_lines.append(line)
    description = '\n'.join(description_lines) or None
    return {'date_times': date_times, 'venue': clean_text(venue), 'description': description}


def fetch_detail(card):
    try:
        return card, detail_data(request_html(card['url']))
    except (requests.RequestException, ValueError) as error:
        log_message(
            'Concert detail request failed',
            event='crawler_detail_failed',
            level='warning',
            url=card['url'],
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return card, None


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    season_url = STARTING_SEASON_URL
    seen_seasons = set()
    cards_by_occurrence = {}

    while season_url and season_url not in seen_seasons:
        seen_seasons.add(season_url)
        try:
            cards, season_url = season_cards(request_html(season_url, session), season_url)
        except requests.RequestException as error:
            log_message(
                'Season archive request failed',
                event='crawler_archive_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            break
        for card in cards:
            for event_date in card['dates']:
                cards_by_occurrence[(card['url'], event_date)] = card

    unique_cards = {card['url']: card for card in cards_by_occurrence.values()}
    details = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(fetch_detail, card) for card in unique_cards.values()]
        for future in as_completed(futures):
            card, data = future.result()
            details[card['url']] = data

    records = []
    for (url, event_date), card in cards_by_occurrence.items():
        detail = details.get(url)
        if not detail or not detail['venue']:
            continue
        times = {value for value in detail['date_times'].values() if value}
        time_from = detail['date_times'].get(event_date)
        if not time_from and len(times) == 1:
            time_from = next(iter(times))
        records.append({
            'title': card['title'],
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': detail['venue'] or DEFAULT_VENUE,
            'city': DEFAULT_CITY,
            'country_code': 'US',
            'description': detail['description'],
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })

    if not records:
        log_message(
            'No parseable concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=STARTING_SEASON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class OkcphilOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='okcphil_org',
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
    OkcphilOrgCrawler().run()


if __name__ == '__main__':
    main()
