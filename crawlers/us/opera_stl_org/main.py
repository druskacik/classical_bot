import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://opera-stl.org/'
SOURCE = 'Opera Theatre of Saint Louis'
CALENDAR_API_URL = urljoin(SOURCE_URL, 'admin/wp-admin/admin-ajax.php')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

VENUE_NORMALIZATIONS = {
    '130 Edgar Road': 'Loretto-Hilton Center',
}


def month_starts(start, count=24):
    year, month = start.year, start.month
    for _ in range(count):
        yield date(year, month, 1)
        month += 1
        if month == 13:
            year += 1
            month = 1


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True) if hasattr(element, 'get_text') else str(element)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_instance(instance):
    try:
        occurrence = instance['post_date']
        event_date = date.fromisoformat(occurrence[:10]).isoformat()
    except (KeyError, TypeError, ValueError):
        return None

    soup = BeautifulSoup(instance.get('content') or '', 'html.parser')
    link = soup.select_one('a.c-cal-perf__link[href]')
    title = clean_text(soup.select_one('.c-cal-perf__title'))
    time_match = re.fullmatch(r'\d{4}-\d{2}-\d{2} ([0-2]\d:[0-5]\d):\d{2}', occurrence)
    url = urljoin(SOURCE_URL, link['href']) if link else ''
    if not all((title, event_date, url, time_match)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_match.group(1),
    }


def detail_fields(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.find('main') or soup
    venue = ''
    info = main.select_one('.c-booking-info__event-information')
    if info:
        for paragraph in info.find_all('p'):
            label = paragraph.find(['b', 'strong'])
            if clean_text(label).lower() != 'venue':
                continue
            link = paragraph.find('a')
            venue = clean_text(link)
            if not venue:
                value = clean_text(paragraph)
                venue = re.sub(r'^venue\s*', '', value, flags=re.IGNORECASE).strip()
            break

    description_node = main.select_one('.c-content, .c-event-intro, .c-whats-on__content')
    description = clean_text(description_node or main)
    return VENUE_NORMALIZATIONS.get(venue, venue), description or None


class OperaStlOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_stl_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        instances = []
        for month in month_starts(date.today()):
            try:
                response = session.get(
                    CALENDAR_API_URL,
                    params={
                        'action': 'basethemeCalendarRequest',
                        'type': 'month',
                        'date': month.isoformat(),
                    },
                    timeout=45,
                )
                response.raise_for_status()
                instances.extend(response.json().get('instances') or [])
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Opera Theatre calendar month',
                    event='crawler_fetch_failed',
                    level='error',
                    url=response.url if 'response' in locals() else CALENDAR_API_URL,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

        details = {}
        records = []
        for instance in instances:
            record = parse_instance(instance)
            if record is None:
                continue
            url = record['url']
            if url not in details:
                try:
                    response = session.get(url, timeout=45)
                    response.raise_for_status()
                    details[url] = detail_fields(response.text)
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Opera Theatre event detail',
                        event='crawler_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    details[url] = ('', None)
            venue, description = details[url]
            # OTSL's dated public calendar is based in St. Louis. Touring and
            # overview pages do not provide a concrete venue and are skipped.
            if not venue:
                continue
            record.update({
                'venue': venue,
                'city': 'St. Louis',
                'country_code': 'US',
                'description': description,
            })
            records.append(record)
        return records


def main():
    return OperaStlOrgCrawler().run()


if __name__ == '__main__':
    main()
