import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.melrosesymphony.org/'
SOURCE = 'Melrose Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

CURRENT_PATHS = (
    'opening-night',
    'holiday-pops',
    'masterworks',
    'family-concert',
    'may-pops',
)

DATE_PATTERN = re.compile(
    r'(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?'
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(20\d{2})'
    r'(?:,?\s+(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?',
    re.IGNORECASE,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    if not value:
        return None
    normalized = value.lower().replace('.', '').replace(' ', '')
    for fmt in ('%I:%M%p', '%I%p'):
        try:
            return datetime.strptime(normalized, fmt).strftime('%H:%M')
        except ValueError:
            pass
    return None


def extract_dates(text):
    dates = []
    for match in DATE_PATTERN.finditer(text):
        try:
            event_date = datetime.strptime(
                f'{match.group(1)} {match.group(2)} {match.group(3)}', '%B %d %Y'
            ).date().isoformat()
        except ValueError:
            continue
        dates.append((event_date, parse_time(match.group(4))))

    # Some archive pages write the first of two performances without repeating
    # the year: "Friday, December 10, and Saturday, December 11, 2021".
    paired = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),?\s+and\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+(\d{1,2}),\s+(20\d{2})',
        text,
        re.IGNORECASE,
    )
    if paired:
        try:
            first = datetime.strptime(
                f'{paired.group(1)} {paired.group(2)} {paired.group(5)}', '%B %d %Y'
            ).date().isoformat()
            if all(item[0] != first for item in dates):
                dates.append((first, None))
        except ValueError:
            pass
    return sorted(set(dates))


def concert_time(text):
    for pattern in (
        r'Concert\s+(?:starts?|begins?)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        r'Concert,\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return parse_time(match.group(1))
    return None


def parse_page(soup, url):
    content = soup.select_one('#content')
    if content is None:
        return []
    title = clean_text(content.select_one('h1'))
    text = clean_text(content)
    # Performance dates are presented at the top of each page. Limiting date
    # parsing to that header avoids turning dated historical anecdotes and
    # cancellation notices in long biographies into extra occurrences.
    dates = extract_dates(text[:800])
    if not title or not dates:
        return []

    if re.search(r'\bMorelli Field\b', text, re.IGNORECASE):
        venue = 'Morelli Field'
    elif re.search(r'\bMemorial Hall\b', text, re.IGNORECASE):
        venue = 'Memorial Hall'
    else:
        return []

    descriptions = [clean_text(block) for block in content.select('.sqs-html-content')]
    description = '\n\n'.join(dict.fromkeys(item for item in descriptions if item)) or None
    shared_time = concert_time(text)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time or shared_time,
            'venue': venue,
            'city': 'Melrose',
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, event_time in dates
    ]


class MelroseSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='melrosesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        urls = {urljoin(SOURCE_URL, path) for path in CURRENT_PATHS}
        archive_url = urljoin(SOURCE_URL, 'concert-archive')

        try:
            response = session.get(archive_url, timeout=45)
            response.raise_for_status()
            archive = BeautifulSoup(response.text, 'html.parser')
            for link in archive.select('#content a[href]'):
                label = clean_text(link).lower()
                url = urljoin(SOURCE_URL, link['href'])
                if (
                    urlparse(url).netloc.endswith('melrosesymphony.org')
                    and 'recap' not in label
                    and 'announcement' not in label
                ):
                    urls.add(url.replace('http://', 'https://', 1))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Melrose Symphony concert archive',
                event='crawler_fetch_failed',
                level='warning',
                url=archive_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        records = []
        for url in sorted(urls):
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Melrose Symphony concert page',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            records.extend(parse_page(BeautifulSoup(response.text, 'html.parser'), url))

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MelroseSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
