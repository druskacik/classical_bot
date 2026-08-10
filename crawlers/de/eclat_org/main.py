import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eclat.org/'
PROGRAMME_URL = urljoin(SOURCE_URL, 'programm/')
ARCHIVE_URL = urljoin(SOURCE_URL, 'archiv/')
SOURCE = 'ECLAT Festival Neue Musik Stuttgart'

HEADERS = {
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_inline(element):
    return re.sub(r'\s+', ' ', clean_text(element)).strip()


def parse_date(value, default_year):
    match = re.search(r'\b(\d{1,2})\.(\d{1,2})\.(?:(\d{2,4}))?', value)
    if not match:
        return None
    year_text = match.group(3)
    year = int(year_text) if year_text else default_year
    if year < 100:
        year += (default_year // 100) * 100
    try:
        return date(year, int(match.group(2)), int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_article(article, default_year):
    link = article.select_one('a[href]')
    title_element = article.select_one('.concert__title')
    meta = article.select_one('.concert__meta')
    date_element = article.select_one('.concert__date')
    if not link or not title_element or not meta or not date_element:
        return None

    title_parts = [clean_inline(element) for element in title_element.select('h1, h2')]
    title = ' — '.join(part for part in title_parts if part)
    event_date = parse_date(clean_text(date_element), default_year)
    url = urljoin(SOURCE_URL, link.get('href', ''))

    time_element = article.select_one('.concert__time')
    time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(time_element))

    meta_parts = meta.select('.concert-meta')
    venue = ''
    for part in meta_parts:
        classes = part.get('class', [])
        if 'concert__index' not in classes and 'concert__date' not in classes \
                and 'concert__time' not in classes:
            venue = clean_inline(part)
            if venue:
                break

    if not title or not event_date or not url or not venue:
        return None

    description_parts = []
    subtitle = clean_text(title_element.select_one('h2'))
    short_description = clean_text(article.select_one('.concert__short-description'))
    if subtitle:
        description_parts.append(subtitle)
    if short_description:
        description_parts.append(short_description)

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        ),
        'venue': venue,
        'city': 'Stuttgart',
        'country_code': 'DE',
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


class EclatOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eclat_org',
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
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)

        try:
            programme_soup = fetch_soup(session, PROGRAMME_URL)
            archive_soup = fetch_soup(session, ARCHIVE_URL)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch ECLAT programme index',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        archive_pages = {}
        for option in archive_soup.select('#archive-switch__select option[value]'):
            year_match = re.search(r'/(\d{4})/?$', option.get('value', ''))
            if year_match:
                archive_pages[urljoin(SOURCE_URL, option['value'])] = int(year_match.group(1))

        default_archive_year = int(archive_soup.select_one('#archive').get('data-year'))
        current_year = default_archive_year + 1
        pages = [(PROGRAMME_URL, current_year, programme_soup)]
        pages.append((ARCHIVE_URL, default_archive_year, archive_soup))
        archive_pages = {
            url: year for url, year in archive_pages.items() if year != default_archive_year
        }

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(fetch_soup, session, url): (url, year)
                for url, year in archive_pages.items()
            }
            for future in as_completed(futures):
                url, year = futures[future]
                try:
                    pages.append((url, year, future.result()))
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch ECLAT archive year',
                        event='crawler_page_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for _, year, soup in pages:
            for article in soup.select('article.component.concert'):
                record = parse_article(article, year)
                if record:
                    records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    EclatOrgCrawler().run()


if __name__ == '__main__':
    main()
