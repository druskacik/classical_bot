import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cabrillomusic.org/'
SOURCE = 'Cabrillo Festival of Contemporary Music'
SITEMAP_URL = f'{SOURCE_URL}mec-events-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

INCLUDED_CATEGORIES = {'Concerts', 'Open Rehearsals'}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_urls(xml):
    soup = BeautifulSoup(xml, 'xml')
    return sorted({
        clean_text(location)
        for location in soup.select('loc')
        if '/events-calendar/' in clean_text(location)
    })


def parse_date(value):
    # Date ranges on this site are season/save-the-date overview records, not
    # concrete occurrences. They must not be reduced to a made-up start event.
    value = re.sub(r'^Date\s+', '', value, flags=re.IGNORECASE)
    value = re.sub(r'\s+Expired!?\s*$', '', value, flags=re.IGNORECASE).strip()
    if ' - ' in value or re.search(r'\b\d{1,2}\s*-\s*\d{1,2}\b', value):
        return None
    match = re.fullmatch(r'([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})', value)
    if not match:
        return None
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = re.sub(r'^Time\s+', '', value, flags=re.IGNORECASE).strip()
    if not value or value.lower() == 'all day':
        return None
    match = re.search(r'\b(\d{1,2}):([0-5]\d)\s*([ap])\.?m\.?', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == 'p':
        hour += 12
    return f'{hour:02d}:{match.group(2)}'


def parse_location(element):
    if element is None:
        return None
    title = element.select_one('.mec-meta-label')
    venue = clean_text(title)
    if not venue:
        links = element.select('a')
        venue = clean_text(links[0]) if links else ''

    address_element = element.select_one('address')
    address = clean_text(address_element)
    if not address:
        address = clean_text(element)
        address = re.sub(r'^Location\s+', '', address, flags=re.IGNORECASE)
        if venue and address.startswith(venue):
            address = address[len(venue):].strip()

    city_match = re.search(
        r'(?:^|,\s*)([A-Za-z][A-Za-z .\'-]+),\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b',
        address,
    )
    city = city_match.group(1).strip() if city_match else ''
    if not venue or not city:
        return None
    return venue, city


def parse_event(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    categories = {
        clean_text(link) for link in soup.select('.mec-events-event-categories a')
    }
    if not categories.intersection(INCLUDED_CATEGORIES):
        return None

    title = clean_text(soup.select_one('h1.mec-single-title'))
    event_date = parse_date(clean_text(soup.select_one('.mec-single-event-date')))
    time_from = parse_time(clean_text(soup.select_one('.mec-single-event-time')))
    location = parse_location(soup.select_one('.mec-single-event-location'))
    description = clean_text(soup.select_one('.mec-single-event-description')) or None

    # A real time is required here because the site's all-day records are
    # festival/season overview placeholders rather than performances.
    if not title or not event_date or not time_from or not location:
        return None
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


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_event(response.text, url)


class CabrillomusicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cabrillomusic_org',
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
        response = requests.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        urls = event_urls(response.text)
        records = []

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_event, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Cabrillo Festival event',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    CabrillomusicOrgCrawler().run()


if __name__ == '__main__':
    main()
