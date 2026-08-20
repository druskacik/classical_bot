import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.vashonopera.org/'
SEASONS_URL = urljoin(SOURCE_URL, 'seasons')
SOURCE = 'Vashon Opera'
CITY = 'Vashon'
COUNTRY_CODE = 'US'

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
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    value = clean_text(value).replace('.', '')
    for pattern in ('%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def production_links(season_soup, season_url):
    season_path = urlparse(season_url).path.rstrip('/') + '/'
    links = []
    for link in season_soup.select('main a[href]'):
        url = urljoin(season_url, link.get('href')).split('#', 1)[0]
        path = urlparse(url).path
        if path.startswith(season_path) and path.rstrip('/') != season_path.rstrip('/'):
            links.append(url)
    return list(dict.fromkeys(links))


def season_venue(season_soup):
    text = clean_text(season_soup.select_one('main'))
    matches = re.findall(r'Coming to ([^:\n]+):', text, re.IGNORECASE)
    venues = {clean_text(value) for value in matches if clean_text(value)}
    return venues.pop() if len(venues) == 1 else None


def production_description(soup):
    parts = []
    for section in soup.select('main section'):
        if section.get('id') == 'artists':
            continue
        text = clean_text(section)
        if text and text not in parts:
            parts.append(text)
    if parts:
        return '\n\n'.join(parts)
    article = soup.select_one('main article')
    return clean_text(article) or None


def parse_production(soup, url, venue):
    title_node = soup.select_one('h1.show-for-sr')
    title = clean_text(title_node)
    if not title or not venue:
        return []

    description = production_description(soup)
    records = []
    for occurrence in soup.select('.production-dates .cal-date'):
        event_date = parse_date(clean_text(occurrence.select_one('.date')))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(clean_text(occurrence.select_one('.time'))),
            'venue': venue,
            'city': CITY,
            'country_code': COUNTRY_CODE,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    seasons_soup = get_soup(session, SEASONS_URL)

    season_urls = []
    for link in seasons_soup.select('main a[href]'):
        url = urljoin(SEASONS_URL, link.get('href')).split('#', 1)[0]
        if re.fullmatch(r'https://www\.vashonopera\.org/seasons/\d{4}-\d{4}-season/?', url):
            season_urls.append(url.rstrip('/'))

    records = []
    for season_url in dict.fromkeys(season_urls):
        try:
            season_soup = get_soup(session, season_url)
            venue = season_venue(season_soup)
            for production_url in production_links(season_soup, season_url):
                production_soup = get_soup(session, production_url)
                records.extend(parse_production(production_soup, production_url, venue))
        except requests.RequestException as error:
            log_message(
                'Season could not be scraped',
                event='crawler_season_failed',
                level='warning',
                url=season_url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    if not records:
        log_message(
            'No production occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASONS_URL,
            record_count=0,
        )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class VashonOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='vashonopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=COUNTRY_CODE,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    VashonOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
