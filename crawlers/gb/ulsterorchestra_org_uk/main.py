import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ulsterorchestra.org.uk/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/whats-on'
SOURCE = 'Ulster Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

CITY_ALIASES = {
    'derry~londonderry': 'Derry',
    'londonderry': 'Derry',
}


def clean_text(value):
    if not value:
        return ''
    raw = str(value)
    text = BeautifulSoup(raw, 'html.parser').get_text('\n', strip=True) if '<' in raw else raw
    text = unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_values(soup, label):
    heading = soup.find(
        lambda tag: tag.name in {'span', 'div', 'h2', 'h3'}
        and clean_text(tag.get_text(' ', strip=True)).lower() == label.lower()
    )
    if not heading:
        return []
    section = heading.find_parent(class_=lambda value: value and 'e-child' in value.split())
    if not section:
        return []
    values = [clean_text(item) for item in section.select('.elementor-post-info__item')]
    return [value for value in values if value]


def parse_datetimes(values):
    results = []
    pattern = re.compile(
        r'(\d{1,2}\s+[A-Za-z]+\s+20\d{2})'
        r'(?:\s+(\d{1,2}[:.]\d{2}))?',
        re.I,
    )
    for value in values:
        for match in pattern.finditer(value):
            try:
                event_date = datetime.strptime(match.group(1), '%d %B %Y').date().isoformat()
            except ValueError:
                continue
            time_from = match.group(2).replace('.', ':') if match.group(2) else None
            if time_from:
                hour, minute = time_from.split(':')
                time_from = f'{int(hour):02d}:{minute}'
            pair = (event_date, time_from)
            if pair not in results:
                results.append(pair)
    return results


def parse_location(value):
    location = clean_text(value)
    if ',' not in location:
        return '', ''
    venue, city = [part.strip() for part in location.rsplit(',', 1)]
    city = CITY_ALIASES.get(city.lower(), city)
    return venue, city


def parse_event(html, event):
    url = clean_text(event.get('link'))
    title = clean_text(event.get('title', {}).get('rendered'))
    soup = BeautifulSoup(html, 'html.parser')
    datetimes = parse_datetimes(labelled_values(soup, 'Dates'))
    locations = labelled_values(soup, 'Venue')
    venue, city = parse_location(locations[0]) if locations else ('', '')
    description = clean_text(event.get('content', {}).get('rendered')) or None
    if not title or not url or not datetimes or not venue or not city:
        return []
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in datetimes
    ]


def fetch_event(event):
    url = clean_text(event.get('link'))
    if not url:
        return []
    response = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
    return parse_event(response.text, event)


class UlsterOrchestraOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ulsterorchestra_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        events = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'asc',
                    '_fields': 'link,title,content',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            events.extend(response.json())
            if page >= int(response.headers.get('X-WP-TotalPages', '1')):
                break
            page += 1

        records = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_event, event): event for event in events}
            for future in as_completed(futures):
                event = futures[future]
                url = clean_text(event.get('link'))
                try:
                    parsed = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Ulster Orchestra event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if parsed:
                    records.extend(parsed)
                else:
                    log_message(
                        'Skipped incomplete Ulster Orchestra event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required date, title, URL, venue, or city is missing',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    UlsterOrchestraOrgUkCrawler().run()


if __name__ == '__main__':
    main()
