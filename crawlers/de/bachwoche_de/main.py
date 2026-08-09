import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.bachwoche.de/'
SOURCE = 'Bachwoche Ansbach'
SITEMAP_URL = urljoin(SOURCE_URL, 'de/sitemap.html')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def programme_pages(soup):
    pages = set()
    for link in soup.select('a[href*="programm-"][href$="alle-termine.html"]'):
        url = urljoin(SITEMAP_URL, link.get('href', ''))
        match = re.search(r'/programm-(20\d{2})/alle-termine\.html$', url)
        if match:
            pages.add((int(match.group(1)), url))
    return sorted(pages)


def resolve_location(value):
    venue = re.sub(r'\n.*', '', clean_text(value)).strip()
    if not venue:
        return None

    normalized = venue.lower()
    if 'schwabach' in normalized:
        city = 'Schwabach'
    elif 'heilsbronn' in normalized:
        city = 'Heilsbronn'
    else:
        # The festival calendar is based in Ansbach; its remaining named halls
        # and the explicitly addressed Innenstadt/Gemeindezentrum are there.
        city = 'Ansbach'
    return venue, city


def parse_event(wrapper, year, listing_url):
    title = clean_text(wrapper.select_one('.title'))
    day_month = clean_text(wrapper.select_one('.dayMonth')) or wrapper.get('data-day', '')
    date_match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})\.', day_month)
    detail_link = wrapper.select_one('.eventFooter a[href*="konzertkategorien/"]')
    location = resolve_location(wrapper.select_one('.ortBlock'))
    if not title or not date_match or detail_link is None or not location:
        return None

    try:
        event_date = date(year, int(date_match.group(2)), int(date_match.group(1))).isoformat()
    except ValueError:
        return None

    time_text = clean_text(wrapper.select_one('.badge .year, .badge .time'))
    time_match = re.fullmatch(r'(\d{1,2}):(\d{2})', time_text)
    time_from = None
    if time_match and int(time_match.group(1)) < 24 and int(time_match.group(2)) < 60:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    description_parts = []
    for block in wrapper.select('.programmHiddenContent .contentBlock'):
        text = clean_text(block)
        if text and text not in description_parts:
            description_parts.append(text)

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': urljoin(listing_url, detail_link['href']),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class BachwocheDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='bachwoche_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
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
        current_url = SITEMAP_URL
        try:
            response = session.get(current_url, timeout=45)
            response.raise_for_status()
            pages = programme_pages(BeautifulSoup(response.content, 'html.parser'))
            if not pages:
                raise ValueError('No programme overview pages found in sitemap')

            records = []
            for year, url in pages:
                current_url = url
                response = session.get(current_url, timeout=45)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')
                for wrapper in soup.select('.eventList .eventWrapper'):
                    record = parse_event(wrapper, year, url)
                    if record:
                        records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Bachwoche programme',
                event='crawler_fetch_failed',
                level='error',
                url=current_url,
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
    BachwocheDeCrawler().run()


if __name__ == '__main__':
    main()
