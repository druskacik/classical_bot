import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gfsymphony.org/'
TICKETING_URL = 'https://app.arts-people.com/index.php?ticketing=gfs'
ALUMNI_CONCERT_URL = urljoin(SOURCE_URL, 'alumni-concert')
SOURCE = 'Great Falls Symphony'
CITY = 'Great Falls'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = re.sub(r'^(Tues|Thurs)\b', lambda match: match.group(1)[:3], value.strip())
    try:
        return datetime.strptime(value, '%a %b %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    try:
        return datetime.strptime(value.strip().upper(), '%I:%M %p').strftime('%H:%M')
    except ValueError:
        return None


def card_description(description_node):
    copy = BeautifulSoup(str(description_node), 'html.parser')
    first_heading = copy.find('h3')
    first_list = copy.find('ul')
    if first_heading:
        first_heading.decompose()
    if first_list:
        first_list.decompose()

    for link in copy.find_all('a'):
        if 'TICKET' in clean_text(link).upper() or 'SEASON INFO' in clean_text(link).upper():
            parent = link.find_parent(['h3', 'h4', 'h5', 'p'])
            (parent or link).decompose()
    for paragraph in copy.find_all(['p', 'h5']):
        text = clean_text(paragraph)
        if re.match(r'^(Included in|Season Sponsor|Concert Sponsor|Single Tickets|Weeknight concerts)', text, re.I):
            paragraph.decompose()

    return clean_text(copy) or None


def parse_ticketing_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    table = soup.select_one('#TBLshows_cacheable')
    if not table:
        return records

    for row in table.select('tr'):
        description_node = row.select_one('.show_text_div #description')
        ticket_link = row.select_one('a[href*="show="]')
        if not description_node or not ticket_link:
            continue

        title_node = description_node.find('h3')
        metadata = description_node.find('ul')
        values = [clean_text(item) for item in metadata.find_all('li', recursive=False)] if metadata else []
        if not title_node or len(values) < 3:
            continue

        title = clean_text(title_node)
        date = parse_date(values[0])
        time_from = parse_time(values[1])
        venue = values[2]
        if not title or not date or not venue:
            continue

        records.append({
            'title': title,
            'date': date,
            'url': urljoin(TICKETING_URL, ticket_link.get('href')),
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': card_description(description_node),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_alumni_concert_page(html):
    """Parse the one concrete past concert retained in the site's page archive."""
    soup = BeautifulSoup(html, 'html.parser')
    text = clean_text(soup.select_one('main') or soup.body)
    if 'January 2, 2022' not in text or '7:00pm - free concert for the public' not in text.lower():
        return []
    return [{
        'title': 'Great Falls Symphony Youth Orchestra Alumni Concert',
        'date': '2022-01-02',
        'url': ALUMNI_CONCERT_URL,
        'time_from': '19:00',
        'venue': 'Mansfield Theatre',
        'city': CITY,
        'country_code': 'US',
        'description': text,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }]


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(TICKETING_URL, timeout=45)
    response.raise_for_status()
    records = parse_ticketing_page(response.text)

    try:
        archive_response = session.get(ALUMNI_CONCERT_URL, timeout=45)
        archive_response.raise_for_status()
        records.extend(parse_alumni_concert_page(archive_response.text))
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Great Falls Symphony archived concert page',
            event='crawler_archive_fetch_failed',
            level='warning',
            url=ALUMNI_CONCERT_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    if not records:
        log_message(
            'No Great Falls Symphony concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=TICKETING_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class GfsymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gfsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    GfsymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
