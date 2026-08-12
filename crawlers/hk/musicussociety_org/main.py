import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicussociety.org/'
SOURCE = 'Musicus Society'
FEED_URLS = (
    urljoin(SOURCE_URL, 'en/All-Coming-Events.html'),
    urljoin(SOURCE_URL, 'en/All-Past-Events.html'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-HK,en;q=0.9',
}
MONTHS = {
    name.lower(): number for number, name in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June',
         'July', 'August', 'September', 'October', 'November', 'December'),
        1,
    )
}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})

FOREIGN_LOCATIONS = (
    ('shenzhen', 'Shenzhen', 'CN'), ('shanghai', 'Shanghai', 'CN'),
    ('hangzhou', 'Hangzhou', 'CN'), ('huizhou', 'Huizhou', 'CN'),
    ('dongguan', 'Dongguan', 'CN'), ('zhuhai', 'Zhuhai', 'CN'),
    ('kuhmo', 'Kuhmo', 'FI'), ('kauniainen', 'Kauniainen', 'FI'),
    ('espoo', 'Espoo', 'FI'),
    ('london', 'London', 'GB'), ('salzburg', 'Salzburg', 'AT'),
    ('paris', 'Paris', 'FR'), ('budapest', 'Budapest', 'HU'),
    ('weimar', 'Weimar', 'DE'), ('berlin', 'Berlin', 'DE'),
    ('rottenburg', 'Rottenburg', 'DE'), ('trondheim', 'Trondheim', 'NO'),
    ('banff', 'Banff', 'CA'),
)


def clean_text(node, separator=' '):
    if node is None:
        return ''
    text = node.get_text(separator, strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u2013', '-').replace('\u2014', '-')
    if separator == '\n':
        return re.sub(r' *\n *', '\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?(?!\w)', value, re.I)
    if not match:
        match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', value)
        return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == 'p' else 0)
    return f'{hour:02d}:{match.group(2) or "00"}'


def _date_range(start, end):
    # Longer periods on this site are tour/festival overview pages, not a claim
    # that a performance occurs on every intervening calendar day.
    if (end - start).days > 2:
        return []
    return [(start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)]


def parse_dates(value):
    """Expand the concrete dates advertised by an event card."""
    value = clean_text(value).replace('.', ' ')
    month_pattern = '|'.join(sorted(MONTHS, key=len, reverse=True))

    cross = re.search(
        rf'\b(\d{{1,2}})\s+({month_pattern})\s*-\s*(\d{{1,2}})\s+({month_pattern})\s+(20\d{{2}})\b',
        value, re.I,
    )
    if cross:
        year = int(cross.group(5))
        try:
            return _date_range(
                date(year, MONTHS[cross.group(2).lower()], int(cross.group(1))),
                date(year, MONTHS[cross.group(4).lower()], int(cross.group(3))),
            )
        except ValueError:
            return []

    match = re.search(
        rf'\b([\d, &-]+?)\s+({month_pattern})\s+(20\d{{2}})\b', value, re.I,
    )
    if not match:
        return []
    month = MONTHS[match.group(2).lower()]
    year = int(match.group(3))
    dates = []
    for part in re.split(r'\s*(?:,|&|and)\s*', match.group(1).strip()):
        range_match = re.fullmatch(r'(\d{1,2})\s*-\s*(\d{1,2})', part)
        if range_match:
            if int(range_match.group(2)) - int(range_match.group(1)) > 2:
                return []
            days = range(int(range_match.group(1)), int(range_match.group(2)) + 1)
        elif re.fullmatch(r'\d{1,2}', part):
            days = [int(part)]
        else:
            return []
        for day in days:
            try:
                dates.append(date(year, month, day).isoformat())
            except ValueError:
                return []
    return dates


def parse_location(value):
    venue = re.split(r'\bAddress\s*:', clean_text(value), maxsplit=1, flags=re.I)[0].strip(' ,|')
    lower = venue.lower()
    if not venue or any(term in lower for term in ('online', 'zoom', 'webinar')):
        return None

    matches = {(city, code) for needle, city, code in FOREIGN_LOCATIONS if needle in lower}
    if len(matches) == 1:
        city, country_code = matches.pop()
        return venue, city, country_code
    if len(matches) > 1 or re.search(r'\b(?:and|&)\b', venue, re.I) and 'hong kong' not in lower:
        return None
    if re.search(
        r'\b(?:china|germany|canada|finland|norway|austria|france|hungary|'
        r'taiwan|korea|japan|singapore|united kingdom|uk|usa|united states)\b',
        lower,
    ):
        return None
    return venue, 'Hong Kong', 'HK'


def card_data(card):
    onclick = card.get('onclick', '')
    link_match = re.search(r"goUrl\(['\"]([^'\"]+)", onclick)
    title = clean_text(card.select_one('.concert-name'))
    dates = parse_dates(clean_text(card.select_one('.info-d .date, .date')))
    location = parse_location(clean_text(card.select_one('.info-d .venue, .venue')))
    if not title or not link_match or not dates or not location:
        return None
    return {
        'title': title,
        'url': urljoin(SOURCE_URL, link_match.group(1)),
        'dates': dates,
        'location': location,
        'summary': clean_text(card.select_one('.info-d .date, .date')),
    }


def fetch_description(item):
    try:
        response = requests.get(item['url'], headers=HEADERS, timeout=25)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Musicus Society event detail',
            event='crawler_detail_fetch_failed', level='warning', url=item['url'],
            error_type=type(error).__name__, error_message=str(error),
        )
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    return clean_text(soup.select_one('.general .widget.rec'), separator='\n') or None


class MusicussocietyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicussociety_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='HK',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        items_by_url = {}
        for feed_url in FEED_URLS:
            try:
                response = requests.get(feed_url, headers=HEADERS, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Musicus Society event feed',
                    event='crawler_fetch_failed', level='error', url=feed_url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                raise
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.select('.concert-item'):
                item = card_data(card)
                if item:
                    items_by_url[item['url']] = item

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = {executor.submit(fetch_description, item): item
                       for item in items_by_url.values()}
            for future in as_completed(futures):
                futures[future]['description'] = future.result()

        records = []
        for item in items_by_url.values():
            venue, city, country_code = item['location']
            time_from = parse_time(item['summary'])
            for event_date in item['dates']:
                records.append({
                    'title': item['title'], 'date': event_date, 'url': item['url'],
                    'time_from': time_from, 'venue': venue, 'city': city,
                    'country_code': country_code, 'description': item.get('description'),
                    'source_url': SOURCE_URL, 'source': SOURCE,
                })
        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    return MusicussocietyOrgCrawler().run()


if __name__ == '__main__':
    main()
