import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.fairfaxsymphony.org/'
SEASON_URL = urljoin(SOURCE_URL, '2627-season-overview')
SOURCE = 'Fairfax Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
    r'([A-Za-z]+)\s+(\d{1,2}),\s+(20\d{2})\s*\|\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b',
    re.I,
)

VENUES = {
    'Center for the Arts at George Mason University': 'Fairfax',
    'Harris Theatre at George Mason University': 'Fairfax',
    'Capital One Hall - Tysons': 'Tysons',
    'Capital One Hall': 'Tysons',
}


def clean_text(value):
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value or '')
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(hour, minute, meridiem):
    parsed = datetime.strptime(
        f'{hour}:{minute or "00"} {meridiem.upper()}', '%I:%M %p'
    )
    return parsed.strftime('%H:%M')


def page_title(soup):
    meta = soup.select_one('meta[property="og:title"]')
    title = clean_text(meta.get('content')) if meta else ''
    if not title and soup.title:
        title = clean_text(soup.title)
    return re.sub(r'\s+[—|]\s+Fairfax Symphony.*$', '', title).strip()


def find_venue(text, start=0):
    matches = []
    lowered = text.lower()
    for venue, city in VENUES.items():
        position = lowered.find(venue.lower(), start)
        if position >= 0:
            matches.append((position, venue, city))
    if not matches:
        return '', ''
    _, venue, city = min(matches)
    return venue, city


def description_from_main(soup):
    main = soup.select_one('main')
    if not main:
        return None
    parts = []
    for element in main.select('p, h2, h3, h4'):
        text = clean_text(element)
        if len(text) >= 20 and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def parse_fairfax_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    text = clean_text(main)
    title = page_title(soup)
    dates = []
    for match in DATE_RE.finditer(text):
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        value = (event_date, parse_time(match.group(4), match.group(5), match.group(6)))
        if value not in dates:
            dates.append(value)
    venue, city = find_venue(text, DATE_RE.search(text).end() if DATE_RE.search(text) else 0)
    if not title or not venue or not city or not dates:
        return []
    description = description_from_main(soup)
    return [
        {
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
        }
        for event_date, time_from in dates
    ]


def parse_capital_one_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    event_data = clean_text(soup.select_one('#legends-event-json'))
    title_match = re.search(r'"EventName"\s*:\s*"([^"]+)"', event_data)
    date_match = re.search(r'"EventDate"\s*:\s*"([A-Za-z]+\s+\d{1,2},\s+20\d{2})"', event_data)
    time_match = re.search(
        r'"EventStartTime"\s*:\s*"\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)"',
        event_data,
        re.I,
    )
    title = title_match.group(1) if title_match else page_title(soup)
    if not title or not date_match or not time_match:
        return []
    try:
        event_date = datetime.strptime(date_match.group(1), '%B %d, %Y').date().isoformat()
    except ValueError:
        return []
    description = description_from_main(soup)
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_match.group(1), time_match.group(2), time_match.group(3)),
        'venue': 'Capital One Hall',
        'city': 'Tysons',
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def event_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = set()
    for anchor in soup.select('a[href]'):
        url = urljoin(SEASON_URL, anchor.get('href')).split('#', 1)[0]
        parsed = urlparse(url)
        if parsed.netloc == 'www.fairfaxsymphony.org':
            if re.search(r'(?:january|february|march|april|may|june|july|august|september|october|november|december)-\d|blade-runner', parsed.path, re.I):
                links.add(url)
        elif parsed.netloc == 'www.capitalonehall.com' and '/events/detail/' in parsed.path:
            links.add(url)
    return sorted(links)


class FairfaxSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='fairfaxsymphony_org',
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
        records = []
        for url in event_links(response.text):
            try:
                detail = requests.get(url, headers=HEADERS, timeout=45)
                detail.raise_for_status()
                if urlparse(url).netloc == 'www.capitalonehall.com':
                    parsed = parse_capital_one_event(detail.text, url)
                else:
                    parsed = parse_fairfax_event(detail.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Fairfax Symphony concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if not parsed:
                log_message(
                    'Skipped incomplete Fairfax Symphony concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, venue, or city is missing',
                )
            records.extend(parsed)
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    FairfaxSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
