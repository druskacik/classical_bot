import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.rapidessymphony.org/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Rapides Symphony Orchestra'

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
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def parse_date_and_time(value):
    value = clean_text(value).replace('.', '')
    parts = [part.strip() for part in value.split('|')]
    if len(parts) < 4:
        return None, None

    try:
        event_date = datetime.strptime(
            ' '.join(parts[1:3]), '%B %d %Y'
        ).date().isoformat()
    except ValueError:
        return None, None

    time_from = None
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            time_from = datetime.strptime(parts[3].upper(), pattern).strftime('%H:%M')
            break
        except ValueError:
            continue
    return event_date, time_from


def parse_location(description_node):
    venue_node = description_node.find(['strong', 'b'])
    venue = clean_text(venue_node.get_text(' ', strip=True)) if venue_node else ''
    text = clean_text(description_node.get_text(' ', strip=True))
    city_match = re.search(r'\bAlexandria\s*,\s*LA\b', text, re.IGNORECASE)
    city = 'Alexandria' if city_match else ''
    return venue.rstrip(' -'), city


def parse_description(description_node):
    text = clean_text(description_node.get_text(' ', strip=True))
    # Detail descriptions begin with the venue and street address. Retain the
    # artist and programme copy that follows, including composer/work names.
    text = re.sub(
        r'^.*?\bAlexandria\s*,\s*LA\b\s*',
        '',
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text or None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    wrapper = soup.select_one('.Event_Detail_Wrapper')
    if not wrapper:
        return None

    title_node = wrapper.select_one('.Title')
    date_node = wrapper.select_one('.Date')
    description_node = wrapper.select_one('.Desc')
    if not title_node or not date_node or not description_node:
        return None

    title = clean_text(title_node.get_text(' ', strip=True))
    event_date, time_from = parse_date_and_time(date_node.get_text(' ', strip=True))
    venue, city = parse_location(description_node)
    if not title or not event_date or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': parse_description(description_node),
    }


def listing_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for card in soup.select('.Idx_Events_Wrap .Col'):
        link = card.select_one('.Event_Btn_Wrap a[href]')
        if not link:
            continue
        url = urljoin(EVENTS_URL, link['href'])
        if url.startswith(SOURCE_URL) and url not in urls:
            urls.append(url)
    return urls


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)

    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    urls = listing_urls(response.text)
    records = []

    for url in urls:
        try:
            detail_response = session.get(url, timeout=45)
            detail_response.raise_for_status()
            record = parse_detail(detail_response.text, detail_response.url)
        except requests.RequestException as error:
            log_message(
                'Concert detail request failed',
                event='crawler_detail_request_failed',
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
                'Concert detail could not be parsed',
                event='crawler_detail_parse_failed',
                level='warning',
                url=url,
            )

    if not records:
        log_message(
            'No concerts found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class RapidesSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='rapidessymphony_org',
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
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    RapidesSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
