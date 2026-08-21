import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://eugendoga.com/'
SOURCE = 'Eugen Doga'
CONCERT_TAG_URL = urljoin(SOURCE_URL, 'ro/tags/concert')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'ro,en;q=0.8',
}

MONTHS = {
    'ianuarie': 1, 'january': 1, 'februarie': 2, 'february': 2,
    'martie': 3, 'march': 3, 'aprilie': 4, 'april': 4, 'mai': 5,
    'may': 5, 'iunie': 6, 'june': 6, 'iulie': 7, 'july': 7,
    'august': 8, 'septembrie': 9, 'september': 9, 'octombrie': 10,
    'october': 10, 'noiembrie': 11, 'november': 11,
    'decembrie': 12, 'december': 12,
}

# The archive has no structured location fields.  These are unambiguous,
# recurring venue names found on its concert pages; they also prevent a city
# name or an address from being used as a venue.
VENUES = [
    (r'Teatrul Verde', 'Teatrul Verde', 'Chișinău', 'MD'),
    (r'Casa de Cultur[ăa] a Studen[țt]ilor(?: din)? Ia[șs]i',
     'Casa de Cultură a Studenților Iași', 'Iași', 'RO'),
    (r'Teatrul Na[țt]ional(?: din)? Ia[șs]i', 'Teatrul Național Iași', 'Iași', 'RO'),
    (r'Ateneul Rom[aâ]n', 'Ateneul Român', 'București', 'RO'),
    (r'Palatul Na[țt]ional(?:\s*[„\"]?Nicolae Sulac[”\"]?)?',
     'Palatul Național „Nicolae Sulac”', 'Chișinău', 'MD'),
    (r'Sala cu Org[ăa]', 'Sala cu Orgă', 'Chișinău', 'MD'),
    (r'Cetatea Soroca|fort[ăa]rea[țt]a medieval[ăa] din Soroca',
     'Cetatea Soroca', 'Soroca', 'MD'),
    (r'Teatrul Na[țt]ional de Oper[ăa] [șs]i Balet(?:\s*[„\"]?Maria Bie[șs]u[”\"]?)?',
     'Teatrul Național de Operă și Balet „Maria Bieșu”', 'Chișinău', 'MD'),
]


def clean_text(value):
    text = unicodedata.normalize('NFC', value or '').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def publication_year(title):
    years = re.findall(r'\b(20\d{2})\b', title)
    return int(years[-1]) if years else None


def parse_event_date(text, title):
    year_hint = publication_year(title)
    patterns = [
        r'(?i)(?:data\s*:\s*|va avea loc(?:\s+pe data de)?\s+|organizat pe\s+|'
        r'publicul este invitat[^.]{0,100}?pe\s+|vineri,\s*|joi,\s*|duminic[ăa],\s*)'
        r'(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?',
        r'(?i)(?:concert\s*[-–:]?\s*|data\s*:\s*)(\d{1,2})[./](\d{1,2})[./](20\d{2})',
        r'(?i)\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?',
    ]
    for pattern_index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text):
            try:
                if pattern_index != 1:
                    year = int(match.group(3)) if match.group(3) else year_hint
                    if not year:
                        continue
                    value = date(year, MONTHS[match.group(2).lower()], int(match.group(1)))
                else:
                    value = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
                return value.isoformat()
            except ValueError:
                continue
    return None


def parse_time(text):
    match = re.search(r'(?i)(?:ora|încep(?:ând|and) cu ora)\s*[:\-]?\s*(\d{1,2})[.:\-](\d{2})', text)
    if not match:
        # Several ticket-style pages use "Saturday, 19:00".
        match = re.search(
            r'(?i)(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|'
            r'luni|mar[țt]i|miercuri|joi|vineri|s[âa]mb[ăa]t[ăa]|duminic[ăa]),?\s*'
            r'(\d{1,2}):(\d{2})', text,
        )
    if not match:
        return None
    hour, minute = map(int, match.groups())
    return f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None


def parse_location(text):
    for pattern, venue, city, country_code in VENUES:
        if re.search(pattern, text, re.I):
            return venue, city, country_code
    return None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    heading = soup.select_one('h1')
    body = soup.select_one('.field-name-body')
    if not heading or not body:
        return None
    title = clean_text(heading.get_text(' ', strip=True))
    description = clean_text(body.get_text('\n', strip=True))
    event_date = parse_event_date(description, title)
    location = parse_location(f'{title}\n{description}')
    if not title or not description or not event_date or not location:
        return None
    venue, city, country_code = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EugenDogaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eugendoga_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        response = session.get(CONCERT_TAG_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = []
        for link in soup.select('.view-content a[href]'):
            url = urljoin(CONCERT_TAG_URL, link.get('href'))
            if url.startswith(urljoin(SOURCE_URL, 'ro/')) and url not in urls:
                urls.append(url)

        records = []
        for url in urls:
            try:
                detail_response = session.get(url, headers=HEADERS, timeout=45)
                detail_response.raise_for_status()
                record = parse_detail(detail_response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Eugen Doga concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped Eugen Doga article without complete event data',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='A defensible event date, venue, or city was not found',
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    EugenDogaComCrawler().run()


if __name__ == '__main__':
    main()
