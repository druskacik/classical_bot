import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gwylgregynogfestival.org/'
SOURCE = 'Gŵyl Gregynog Festival'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programme-2022')
CITY = 'Newtown'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'(?P<date>\d{1,2}\s+[A-Za-z]+\s+\d{4}),\s*'
    r'(?P<time>\d{1,2}[.:]\d{2}\s*(?:am|pm))',
    re.IGNORECASE,
)


def clean_text(element):
    if not element:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.content, 'html.parser')


def detail_urls(session):
    soup = get_soup(session, PROGRAMME_URL)
    return sorted({
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('.entry.container a[href]')
        if clean_text(link).lower() == 'more'
    })


def parse_detail(session, url):
    soup = get_soup(session, url)
    entry = soup.select_one('.entry.container')
    if not entry:
        return None

    title_element = entry.find('h2')
    details_element = title_element.find_next('h4') if title_element else None
    title = clean_text(title_element)
    details = clean_text(details_element)
    match = DATE_TIME_RE.search(details)

    # The archive also lists two pre-concert talks. Their first-party detail
    # pages explicitly say that they are free to concert ticketholders.
    english_section = []
    if details_element:
        for element in details_element.find_all_next(['h4', 'p', 'hr']):
            if element.name == 'hr':
                break
            english_section.append(element)
    section_text = '\n'.join(clean_text(element) for element in english_section)
    if (
        not title
        or not match
        or re.search(r'free to concert ticketholders', section_text, re.IGNORECASE)
    ):
        return None

    venue_text = details[match.end():].strip(' ,\n')
    venue = venue_text.split('\n', 1)[0].strip()
    if not venue:
        return None

    description_parts = []
    for element in english_section:
        text = clean_text(element)
        if not text:
            continue
        if element.name == 'p' and re.match(r'^(Tickets?|Adults?|Young people)\b', text, re.I):
            break
        description_parts.append(text)

    try:
        start = datetime.strptime(
            f"{match.group('date')} {match.group('time').replace('.', ':').upper()}",
            '%d %B %Y %I:%M%p',
        )
    except ValueError:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': CITY,
        'country_code': 'GB',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url in detail_urls(session):
        try:
            record = parse_detail(session, url)
            if record:
                records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape Gŵyl Gregynog event detail',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda item: (item['date'], item['time_from'], item['title']))


class GwylGregynogFestivalOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gwylgregynogfestival_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GwylGregynogFestivalOrgCrawler().run()


if __name__ == '__main__':
    main()
