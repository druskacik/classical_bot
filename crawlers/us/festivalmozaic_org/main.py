import math
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.festivalmozaic.org/'
SOURCE = 'Festival Mozaic'
ARCHIVE_URL = urljoin(SOURCE_URL, 'past-festivals')
ARCHIVE_API_URL = urljoin(SOURCE_URL, 'other-shows-ajax-pagination')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_and_time(value):
    # Date ranges on this site are festival/season overview pages, not concrete
    # occurrences, so deliberately accept one date only.
    matches = re.findall(r'[A-Z][a-z]+\s+\d{1,2},\s+20\d{2}', value)
    if len(matches) != 1:
        return None, None
    try:
        event_date = datetime.strptime(matches[0], '%B %d, %Y').date().isoformat()
    except ValueError:
        return None, None

    time_match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([AP]M)\b', value, re.I)
    time_from = None
    if time_match:
        time_from = datetime.strptime(
            ' '.join(time_match.groups()), '%I %M %p'
        ).strftime('%H:%M')
    return event_date, time_from


def parse_location(container):
    location = container.select_one('.location-item-full-width-more a')
    lines = [line.strip() for line in clean_text(location).splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    city = None
    for line in reversed(lines):
        match = re.search(r'^(.+?),\s*CA(?:\s+\d{5}(?:-\d{4})?)?$', line)
        if match:
            city = match.group(1).strip()
            break
    if not city:
        return None

    venue = lines[0]
    if not venue or venue.casefold() == city.casefold():
        return None
    return venue, city


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    container = soup.select_one('.sd-tab-overview')
    title = clean_text(container.select_one('h1')) if container else ''
    date_element = container.select_one('i.icon-calendar3') if container else None
    date_text = clean_text(date_element.parent) if date_element else ''
    event_date, time_from = parse_date_and_time(date_text)
    location = parse_location(container) if container else None
    if not title or not event_date or not location:
        return None

    content = container.select_one('.col-md-8.order-md-first')
    if content:
        content = BeautifulSoup(str(content), 'html.parser')
        for element in content.select('p, ul.list-group'):
            element.decompose()
        description = clean_text(content) or None
    else:
        description = None

    venue, city = location
    return {
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


def detail_urls(soup):
    return {
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('a[href*="/show-details/"]')
    }


class FestivalMozaicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalmozaic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
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
        try:
            response = session.get(ARCHIVE_URL, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            form = soup.select_one('#gallery-ajax-form')
            token = soup.select_one('meta[name="csrf-token"]')
            if form is None or token is None:
                raise ValueError('Could not find archive pagination metadata')

            form_data = {
                field['name']: field.get('value', '')
                for field in form.select('input[name]')
            }
            total = int(form_data['totalgallery'])
            limit = int(form_data['limit'])
            urls = detail_urls(soup)
            for page in range(2, math.ceil(total / limit) + 1):
                page_data = {**form_data, 'offset': str(page)}
                page_response = session.post(
                    ARCHIVE_API_URL,
                    data=page_data,
                    headers={'X-CSRF-TOKEN': token['content']},
                    timeout=45,
                )
                page_response.raise_for_status()
                urls.update(detail_urls(BeautifulSoup(page_response.text, 'html.parser')))

            records = []
            for url in sorted(urls):
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
                record = parse_detail(detail_response.text, url)
                if record:
                    records.append(record)
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            log_message(
                'Failed to scrape Festival Mozaic archive',
                event='crawler_fetch_failed',
                level='error',
                url=ARCHIVE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    FestivalMozaicOrgCrawler().run()


if __name__ == '__main__':
    main()
