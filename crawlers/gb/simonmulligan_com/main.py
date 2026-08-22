import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'http://simonmulligan.com/'
SOURCE = 'Simon Mulligan'
PAGES = ('schedule', 'news')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

US_LOCATION_NAMES = {
    'allentown, pennsylvania': 'Allentown',
    'college park, maryland': 'College Park',
    'manhattan': 'New York City',
    'new york': 'New York City',
    'new york city': 'New York City',
    'savannah, georgia': 'Savannah',
}

PERFORMANCE_RE = re.compile(
    r'\b(?:concerts?|perform(?:ance|ances|ed|ing)?|recitals?|premiere|first performance)\b',
    re.I,
)
NON_EVENT_RE = re.compile(
    r'\b(?:album|compact disc|faculty|podcast|recorded|recording|released|streaming)\b',
    re.I,
)
VENUE_RE = re.compile(
    r'\b(?:at|to)\s+(?:the\s+)?'
    r"((?:[A-Z][\w’'&.-]*\s+){0,5}"
    r'(?:Hall|Institute|Center|Centre|Theatre|Theater|Church|Cathedral|Museum|Club))\b'
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date(value):
    for pattern in ('%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            continue
    return None


def parse_location(value):
    normalized = re.sub(r'\s+', ' ', value).strip().casefold()
    city = US_LOCATION_NAMES.get(normalized)
    if city:
        return city, 'US'
    if re.fullmatch(r'[^,]+,\s*(?:alabama|alaska|arizona|arkansas|california|colorado|'
                    r'connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|'
                    r'iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|'
                    r'minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|'
                    r'new jersey|new mexico|new york|north carolina|north dakota|ohio|'
                    r'oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|'
                    r'tennessee|texas|utah|vermont|virginia|washington|west virginia|'
                    r'wisconsin|wyoming)', normalized):
        return value.split(',', 1)[0].strip(), 'US'
    return None


def parse_time(value):
    match = re.search(r'\b(1[0-2]|0?[1-9])(?:[.:]([0-5]\d))?\s*(a\.?m\.?|p\.?m\.?)\b', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower().startswith('p'):
        hour += 12
    return f'{hour:02d}:{match.group(2) or "00"}'


def extract_venue(description):
    match = VENUE_RE.search(description)
    return re.sub(r'\s+', ' ', match.group(1)).strip() if match else None


def make_title(description):
    sentence = re.split(r'(?<=[.!?])\s+', description, maxsplit=1)[0].strip()
    return sentence.rstrip('.') if sentence else None


def parse_item(item, page_url):
    paragraphs = item.find_all('p', recursive=False)
    if len(paragraphs) < 3:
        return None

    event_date = parse_date(clean_text(item.select_one('.pull-left.lead')))
    location = parse_location(clean_text(item.select_one('.pull-right.lead')))
    description = clean_text(paragraphs[-1])
    if not event_date or not location or not PERFORMANCE_RE.search(description):
        return None
    if NON_EVENT_RE.search(description) and not re.search(r'\b(?:concert|performance|recital)\b', description, re.I):
        return None

    venue = extract_venue(description)
    title = make_title(description)
    if not venue or not title:
        return None

    city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': page_url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class SimonMulliganComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simonmulligan_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for path in PAGES:
            page_url = urljoin(SOURCE_URL, path)
            try:
                response = session.get(page_url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Simon Mulligan events page',
                    event='crawler_fetch_failed',
                    level='error',
                    url=page_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            soup = BeautifulSoup(response.text, 'html.parser')
            for item in soup.select('.concert'):
                record = parse_item(item, page_url)
                if record:
                    records.append(record)

        return sorted(records, key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ))


def main():
    SimonMulliganComCrawler().run()


if __name__ == '__main__':
    main()
