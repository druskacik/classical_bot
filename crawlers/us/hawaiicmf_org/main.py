import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://hawaiicmf.org/'
SOURCE = 'Hawaiʻi Chamber Music Festival'

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
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = value.replace('\u202f', ' ').replace('\xa0', ' ').strip()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value.upper(), pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def extract_city(address):
    if address is None:
        return None

    location = clean_text(address)
    match = re.search(r'(?:^|\n)([A-Za-zʻ‘’ .-]+),\s*HI,?\s+\d{5}\b', location)
    if match:
        return match.group(1).strip()

    # All published festival occurrences are in Honolulu, and the organization
    # explicitly describes its programme as taking place throughout Honolulu.
    return 'Honolulu'


def parse_event(article, detail_soup, event_url):
    title = clean_text(article.select_one('.eventlist-title-link'))
    date_element = article.select_one('.eventlist-meta-date time[datetime]')
    venue_element = article.select_one('.eventlist-meta-address')
    venue = clean_text(venue_element)
    if venue_element:
        map_link = venue_element.select_one('a')
        if map_link:
            map_link.extract()
            venue = clean_text(venue_element)

    event_date = date_element.get('datetime', '').strip() if date_element else ''
    try:
        event_date = datetime.strptime(event_date, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    start = article.select_one('.event-time-localized-start')
    description_element = detail_soup.select_one('.eventitem-column-content')
    description = clean_text(description_element) or clean_text(
        article.select_one('.eventlist-excerpt')
    ) or None
    detail_address = detail_soup.select_one('.eventitem-meta-address')
    city = extract_city(detail_address or venue_element)

    if not title or not venue or not city:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': event_url,
        'time_from': parse_time(clean_text(start)) if start else None,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class HawaiicmfOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='hawaiicmf_org',
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
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            home_response = session.get(SOURCE_URL, timeout=45)
            home_response.raise_for_status()
            home_soup = BeautifulSoup(home_response.text, 'html.parser')
            season_links = {
                urljoin(SOURCE_URL, link['href']).split('#', 1)[0]
                for link in home_soup.select('a[href]')
                if re.fullmatch(r'/\d{4}-season/?', urlparse(link['href']).path)
            }
            if not season_links:
                raise ValueError('Could not find the current festival season page')

            records = []
            for season_url in sorted(season_links):
                response = session.get(season_url, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')

                for article in soup.select('article.eventlist-event'):
                    link = article.select_one('.eventlist-title-link[href]')
                    if link is None:
                        continue
                    event_url = urljoin(season_url, link['href']).split('#', 1)[0]
                    detail_response = session.get(event_url, timeout=45)
                    detail_response.raise_for_status()
                    detail_soup = BeautifulSoup(detail_response.text, 'html.parser')
                    record = parse_event(article, detail_soup, event_url)
                    if record:
                        records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Hawaiʻi Chamber Music Festival events',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
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
    HawaiicmfOrgCrawler().run()


if __name__ == '__main__':
    main()
