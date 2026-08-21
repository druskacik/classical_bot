import re

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.gheorghezamfir.ro/'
SOURCE = 'Gheorghe Zamfir'
SITEMAP_URL = f'{SOURCE_URL}pages-sitemap.xml'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The Wix site has no events collection/API. Its archive consists of hand-built
# pages, several of which are festival overviews or articles without a complete
# date. Only pages describing a concrete, fully locatable performance belong
# here. Keeping the occurrence facts explicit also prevents the Bucharest
# address in the shared footer from being mistaken for a touring venue.
EVENTS = {
    'unforgettable-2025': {
        'title': 'Andrea Bocelli and Gheorghe Zamfir at Unforgettable Festival 2025',
        'date': '2025-09-12',
        'venue': 'Constitution Square',
        'city': 'Bucharest',
        'country_code': 'RO',
    },
    'copy-of-unforgettable-2025': {
        'title': 'Celebrating Zamfir at Unforgettable Festival 2024',
        'date': '2024-09-13',
        'venue': 'Romexpo',
        'city': 'Bucharest',
        'country_code': 'RO',
    },
    'dublin-gheorghe-zamfir-concert': {
        'title': 'Gheorghe Zamfir in Dublin',
        'date': '2018-06-09',
        'venue': 'The Helix',
        'city': 'Dublin',
        'country_code': 'IE',
    },
    'vatican-concert-gheorghe-zamfir': {
        'title': 'Gheorghe Zamfir at the Vatican Christmas Concert',
        'date': '2018-12-15',
        'venue': 'Aula Paolo VI',
        'city': 'Vatican City',
        'country_code': 'VA',
    },
    'gheorghe-zamfir-constitution-square': {
        'title': 'Gheorghe Zamfir - Extraordinary Concert',
        'date': '2018-09-21',
        'venue': 'Constitution Square',
        'city': 'Bucharest',
        'country_code': 'RO',
    },
    'roma-2017': {
        'title': 'Gheorghe Zamfir in Rome',
        'date': '2017-03-04',
        'venue': 'Auditorium Conciliazione',
        'city': 'Rome',
        'country_code': 'IT',
    },
    'zamfir-astana': {
        'title': 'Gheorghe Zamfir at Astana Expo 2017',
        'date': '2017-07-28',
        'venue': 'Expo 2017 Exhibition Complex',
        'city': 'Astana',
        'country_code': 'KZ',
    },
}


def clean_text(value):
    text = str(value or '').replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def page_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup(['script', 'style', 'noscript']):
        element.decompose()
    text = clean_text(soup.get_text('\n', strip=True))

    # Strip the repeated site chrome while retaining the complete article body.
    start_marker = 'Gheorghe Zamfir\n\n'
    if start_marker in text:
        text = text.split(start_marker, 1)[1]
    text = re.split(r'\nBACK\nNEXT(?:\nBACK\nNEXT)?\n', text, maxsplit=1)[0]
    return clean_text(text) or None


class GheorgheZamfirRoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gheorghezamfir_ro',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='RO',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        response = session.get(SITEMAP_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        sitemap = BeautifulSoup(response.text, 'xml')
        published_urls = {node.get_text(strip=True) for node in sitemap.find_all('loc')}

        records = []
        for slug, event in EVENTS.items():
            url = f'{SOURCE_URL}{slug}'
            if url not in published_urls:
                log_message(
                    'Skipped unpublished Gheorghe Zamfir archive page',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='MissingSitemapEntry',
                    error_message='The known event page is absent from the first-party sitemap',
                )
                continue
            try:
                page = session.get(url, headers=HEADERS, timeout=45)
                page.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Gheorghe Zamfir archive page',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue

            records.append({
                **event,
                'url': url,
                'time_from': None,
                'description': page_description(page.text),
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

        return sorted(records, key=lambda item: (item['date'], item['title']))


def main():
    GheorgheZamfirRoCrawler().run()


if __name__ == '__main__':
    main()
