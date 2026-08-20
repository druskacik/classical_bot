import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://wfso.org/'
SOURCE = 'Wichita Falls Symphony Orchestra'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/event'

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
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    try:
        return datetime.strptime(clean_text(value), '%b %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def labelled_container(soup, label):
    heading = soup.find(
        ['h2', 'h3', 'h4', 'h5', 'h6'],
        string=lambda value: value and clean_text(value).casefold() == label.casefold(),
    )
    return heading.parent if heading else None


def parse_detail(html, item):
    soup = BeautifulSoup(html, 'html.parser')

    date_box = labelled_container(soup, 'Event Date')
    time_box = labelled_container(soup, 'Event Time')
    venue_box = labelled_container(soup, 'Venue')
    if not date_box or not venue_box:
        return None

    date_value = date_box.find(['div', 'time'], recursive=False)
    event_date = parse_date(date_value.get_text(' ', strip=True) if date_value else '')

    venue_values = [
        clean_text(node.get_text(' ', strip=True))
        for node in venue_box.find_all(['div', 'time'], recursive=False)
    ]
    venue_values = [value for value in venue_values if value]
    venue = venue_values[0] if venue_values else ''
    address = venue_values[1] if len(venue_values) > 1 else ''

    city_match = re.search(
        r',\s*([^,]+?)(?:,\s*[A-Z]{2})?,?\s+\d{5}(?:-\d{4})?\s*$', address
    )
    city = clean_text(city_match.group(1)) if city_match else ''

    if not event_date or not venue or not city:
        return None

    time_value = time_box.find(['div', 'time'], recursive=False) if time_box else None
    description = clean_text(item.get('content', {}).get('rendered')) or None
    title = clean_text(item.get('title', {}).get('rendered'))
    url = item.get('link', '').strip()
    if not title or not url.startswith(('http://', 'https://')):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(time_value.get_text(' ', strip=True)) if time_value else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(API_URL, params={'per_page': 100, 'page': 1}, timeout=45)
    response.raise_for_status()
    items = response.json()
    total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
    for page in range(2, total_pages + 1):
        page_response = session.get(
            API_URL, params={'per_page': 100, 'page': page}, timeout=45
        )
        page_response.raise_for_status()
        items.extend(page_response.json())

    records = []
    for item in items:
        url = item.get('link', '')
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = parse_detail(detail_response.text, item)
        except requests.RequestException as error:
            log_message(
                'Event detail request failed',
                event='crawler_detail_failed',
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
                'Event skipped because required details were unavailable',
                event='crawler_event_skipped',
                level='warning',
                url=url,
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class WfsoOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wfso_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
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
    WfsoOrgCrawler().run()


if __name__ == '__main__':
    main()
