import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.themosy.org/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar')
SOURCE = 'The Missouri Symphony'

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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (TypeError, json.JSONDecodeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get('@type') == 'Event':
                return item
    return {}


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def event_occurrences(soup, data):
    occurrences = []
    for date_node in soup.select('.eventitem-meta-date time.event-date'):
        event_date = date_node.get('datetime', '').strip()
        time_node = date_node.find_next_sibling(class_='eventitem-meta-time')
        time_from = parse_time(time_node) if time_node else None
        try:
            datetime.strptime(event_date, '%Y-%m-%d')
        except ValueError:
            continue
        occurrences.append((event_date, time_from))

    if occurrences:
        if len(occurrences) == 1 and occurrences[0][1] is None:
            try:
                start = datetime.fromisoformat(data.get('startDate', ''))
            except (TypeError, ValueError):
                pass
            else:
                if start.date().isoformat() == occurrences[0][0]:
                    occurrences[0] = (occurrences[0][0], start.strftime('%H:%M'))
        return occurrences

    start = data.get('startDate', '')
    try:
        parsed = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return []
    return [(parsed.date().isoformat(), parsed.strftime('%H:%M'))]


def event_location(soup, data):
    location = data.get('location') if isinstance(data.get('location'), dict) else {}
    venue = clean_text(location.get('name'))
    address = clean_text(location.get('address'))

    if not venue:
        venue = clean_text(soup.select_one('.eventitem-meta-address-line--title'))
    if not address:
        address = '\n'.join(
            clean_text(node)
            for node in soup.select(
                '.eventitem-meta-address-line:not(.eventitem-meta-address-line--title)'
            )
        )

    city = ''
    lines = [line.strip() for line in address.splitlines() if line.strip()]
    for line in lines:
        match = re.match(r'^([^,]+),\s*(?:Missouri|MO)\b', line, re.I)
        if match:
            city = match.group(1).strip()
            break
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    data = event_json_ld(soup)
    title = clean_text(soup.select_one('.eventitem-title'))
    if not title:
        title = re.sub(r'\s+[—|]\s+The MOSY$', '', clean_text(data.get('name')))
    venue, city = event_location(soup, data)
    description = clean_text(soup.select_one('.eventitem-column-content')) or None

    if not title or not venue or not city:
        return []

    return [
        {
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
        for event_date, time_from in event_occurrences(soup, data)
    ]


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class ThemosyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='themosy_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = sorted({
            urljoin(SOURCE_URL, link.get('href'))
            for link in soup.select('article.eventlist-event a.eventlist-title-link[href]')
        })

        records = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    event_records = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Missouri Symphony event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if not event_records:
                    log_message(
                        'Skipped incomplete Missouri Symphony event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )
                    continue
                records.extend(event_records)

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    ThemosyOrgCrawler().run()


if __name__ == '__main__':
    main()
