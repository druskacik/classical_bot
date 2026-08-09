import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.whistlerchambermusic.ca/'
SOURCE = 'Whistler Chamber Music Society'
SITEMAP_URL = f'{SOURCE_URL}wp-sitemap-posts-concert-1.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-CA,en;q=0.9',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    normalized = re.sub(r'(?<=\d)\s*(?:st|nd|rd|th)\b', '', value, flags=re.IGNORECASE)
    normalized = re.sub(r'\s+', ' ', normalized)
    normalized = re.sub(r'\s+,', ',', normalized)
    normalized = re.sub(r'^[A-Za-z]+,\s*', '', normalized).strip()
    try:
        return datetime.strptime(normalized, '%B %d, %Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(1[0-2]|0?[1-9]):([0-5]\d)\s*([ap]m)\b', value, re.IGNORECASE)
    if not match:
        return None
    parsed = datetime.strptime(''.join(match.groups()), '%I%M%p')
    return parsed.strftime('%H:%M')


def meta_value(soup, label):
    for item in soup.select('.concert__meta-item'):
        subtitle = clean_text(item.select_one('.concert__meta-subtitle'))
        if subtitle.casefold() == label.casefold():
            return clean_text(item.select_one('.concert__meta-content'))
    return ''


def parse_concert(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('.concert__title'))
    event_date = parse_date(meta_value(soup, 'Date'))
    venue = clean_text(soup.select_one('.concert__meta-location'))

    # Every event is published by this Whistler-based, venue-specific series.
    # The address confirms Whistler on nearly all pages; the few abbreviated
    # addresses refer to the same named local venue.
    city = 'Whistler' if venue else None
    if not title or not event_date or not venue or not city:
        return None

    description = clean_text(soup.select_one('.concert__content')) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(meta_value(soup, 'Show Time')),
        'venue': venue,
        'city': city,
        'description': description,
    }


class WhistlerChamberMusicCaCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='whistlerchambermusic_ca',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CA',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Whistler concert sitemap',
                event='crawler_fetch_failed',
                level='error',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.text, 'xml')
        urls = [clean_text(location) for location in sitemap.select('url > loc')]
        records = []
        for url in urls:
            try:
                detail_response = session.get(url, timeout=45)
                detail_response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Whistler concert page',
                    event='crawler_fetch_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            record = parse_concert(detail_response.text, url)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Whistler concert page',
                    event='crawler_record_skipped',
                    level='warning',
                    url=url,
                )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    WhistlerChamberMusicCaCrawler().run()


if __name__ == '__main__':
    main()
