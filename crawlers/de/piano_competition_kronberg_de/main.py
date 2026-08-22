import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://piano-competition-kronberg.de/'
SOURCE = 'International Piano Competition for Young Pianists Kronberg'
START_URL = urljoin(SOURCE_URL, 'en/ergebnisse-2025/')
PRIZE_URL = urljoin(SOURCE_URL, 'en/preistraegerkonzert/')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}


def clean_text(element):
    if element is None:
        return ''
    value = element.get_text('\n', strip=True)
    value = value.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def one_line(value):
    return re.sub(r'\s+', ' ', value).strip()


def parse_date(value):
    numeric = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b', value)
    if numeric:
        day, month, year = map(int, numeric.groups())
    else:
        written = re.search(
            r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
            r'(\d{1,2}),\s*(20\d{2})\b',
            value,
            re.I,
        )
        if not written:
            return None
        month = MONTHS[written.group(1).lower()]
        day, year = int(written.group(2)), int(written.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.m\.', value, re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{int(match.group(2) or 0):02d}'


def location_from_heading(heading):
    lower = heading.lower()
    if 'augustinum' in lower and 'bad soden' in lower:
        return 'Theater hall, Augustinum Bad Soden', 'Bad Soden am Taunus'
    if 'taunus sparkasse bad homburg' in lower:
        return 'Taunus Sparkasse', 'Bad Homburg vor der Höhe'
    if 'hohemark' in lower and 'oberursel' in lower:
        return 'Church hall, Klinik Hohe Mark', 'Oberursel'
    if 'weilburg castle concerts' in lower and 'upper orangery' in lower:
        return 'Upper Orangery, Weilburg Palace', 'Weilburg'
    return None


def parse_follow_up_page(soup, page_url):
    records = []
    for heading in soup.select('h3'):
        heading_text = clean_text(heading)
        event_date = parse_date(heading_text)
        location = location_from_heading(heading_text)
        if not event_date or not location:
            continue
        description = clean_text(heading.parent)
        venue, city = location
        records.append({
            'title': one_line(re.sub(
                r',?\s*(?:\d{1,2}\.\d{1,2}\.20\d{2}|[A-Za-z]+ \d{1,2}, 20\d{2}).*$',
                '',
                heading_text,
            )).strip(' ,'),
            'date': event_date,
            'url': page_url,
            'time_from': parse_time(heading_text),
            'venue': venue,
            'city': city,
            'country_code': 'DE',
            'description': description or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_prize_page(soup, page_url):
    text = clean_text(soup)
    match = re.search(
        r'(?:AWARD-WINNING|PRIZEWINNERS[’\' ]*) CONCERT\s+(\d{2}\.\d{2}\.20\d{2})(?:,\s*([^\n]+))?',
        text,
        re.I,
    )
    if not match:
        return []
    event_date = parse_date(match.group(1))
    return [{
        'title': "Prizewinners' Concert",
        'date': event_date,
        'url': page_url,
        'time_from': parse_time(match.group(2) or ''),
        'venue': 'Casals Forum',
        'city': 'Kronberg im Taunus',
        'country_code': 'DE',
        'description': text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def parse_competition_days(soup, page_url):
    text = clean_text(soup)
    match = re.search(
        r'competition from ([A-Za-z]+) (\d{1,2}) to (\d{1,2}), (20\d{2})'
        r'\s*\(Fri and Sat (\d{1,2})\s*a\.?m\.?\s*-\s*(\d{1,2})\s*p\.?m\.?,\s*Sun (\d{1,2})\s*a\.?m\.?',
        text,
        re.I,
    )
    if not match:
        return []
    month = MONTHS[match.group(1).lower()]
    first = date(int(match.group(4)), month, int(match.group(2)))
    last = date(int(match.group(4)), month, int(match.group(3)))
    records = []
    current = first
    while current <= last:
        start_hour = int(match.group(7) if current == last else match.group(5))
        records.append({
            'title': f'International Piano Competition – {current.strftime("%A")}',
            'date': current.isoformat(),
            'url': page_url,
            'time_from': f'{start_hour:02d}:00',
            'venue': 'Casals Forum',
            'city': 'Kronberg im Taunus',
            'country_code': 'DE',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        current += timedelta(days=1)
    return records


def parse_archived_competition_days(soup, page_url):
    text = clean_text(soup)
    match = re.search(
        r'coming to Kronberg from ([A-Za-z]+) (\d{1,2})\s*[-–]\s*(\d{1,2}),\s*(20\d{2})',
        text,
        re.I,
    )
    if not match:
        return []
    month = MONTHS[match.group(1).lower()]
    first = date(int(match.group(4)), month, int(match.group(2)))
    last = date(int(match.group(4)), month, int(match.group(3)))
    records = []
    current = first
    while current <= last:
        records.append({
            'title': f'International Piano Competition – {current.strftime("%A")}',
            'date': current.isoformat(),
            'url': page_url,
            'time_from': None,
            'venue': 'Casals Forum',
            'city': 'Kronberg im Taunus',
            'country_code': 'DE',
            'description': text or None,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        current += timedelta(days=1)
    return records


class PianoCompetitionKronbergDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='piano_competition_kronberg_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            prize_response = session.get(PRIZE_URL, timeout=45)
            prize_response.raise_for_status()
            prize_soup = BeautifulSoup(prize_response.text, 'html.parser')

            follow_link = prize_soup.select_one('a[href*="anschlusskonzerte"]')
            follow_url = urljoin(PRIZE_URL, follow_link['href']) if follow_link else None
            records = parse_prize_page(prize_soup, PRIZE_URL)
            records.extend(parse_competition_days(prize_soup, PRIZE_URL))

            if follow_url:
                follow_response = session.get(follow_url, timeout=45)
                follow_response.raise_for_status()
                records.extend(parse_follow_up_page(
                    BeautifulSoup(follow_response.text, 'html.parser'), follow_url
                ))

            results_response = session.get(START_URL, timeout=45)
            results_response.raise_for_status()
            records.extend(parse_archived_competition_days(
                BeautifulSoup(results_response.text, 'html.parser'), START_URL
            ))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Piano Competition Kronberg pages',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    PianoCompetitionKronbergDeCrawler().run()


if __name__ == '__main__':
    main()
