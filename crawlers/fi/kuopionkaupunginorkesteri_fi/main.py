import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.kuopionkaupunginorkesteri.fi/'
PROGRAMME_URL = f'{SOURCE_URL}ohjelmisto/'
SOURCE = 'Kuopion kaupunginorkesteri'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def event_urls(session):
    soup = get_soup(session, PROGRAMME_URL)
    return sorted({
        link['href'].split('#', 1)[0]
        for link in soup.select('.event h2 a[href]')
        if '/ohjelmisto/' in link['href']
    })


def parse_heading(value):
    match = re.search(
        r'\b(\d{1,2}\.\d{1,2}\.\d{4})\s+klo\s+'
        r'([01]?\d|2[0-3])(?::([0-5]\d))?\s+(.+)$',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None, None
    try:
        event_date = datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None, None
    time_from = f'{int(match.group(2)):02d}:{match.group(3) or "00"}'
    return event_date, time_from, match.group(4).strip(' ,-')


def resolve_city(venue):
    # The orchestra's programme is a home calendar. Explicit touring cities,
    # when present in the venue string, must override the Kuopio default.
    cities = (
        'Helsinki', 'Espoo', 'Vantaa', 'Lahti', 'Jyväskylä', 'Joensuu',
        'Mikkeli', 'Savonlinna', 'Iisalmi', 'Kajaani', 'Oulu', 'Tampere',
        'Turku', 'Kuopio',
    )
    folded = venue.casefold()
    for city in cities:
        if city.casefold() in folded:
            return city
    return 'Kuopio'


def description_text(article):
    body = BeautifulSoup(str(article), 'html.parser')
    for element in body.select(
        'h1, h2.date, script, style, iframe, .article-some, .gallery, .btn'
    ):
        element.decompose()
    value = clean_text(body)
    return value or None


def parse_concert(soup, url):
    article = soup.select_one('article')
    if not article:
        return None
    title = clean_text(article.select_one('h1'))
    event_date, time_from, venue = parse_heading(clean_text(article.select_one('h2.date')))
    city = resolve_city(venue) if venue else None
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FI',
        'description': description_text(article),
    }


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = event_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = parse_concert(future.result(), url)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Kuopio orchestra concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class KuopionKaupunginorkesteriFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='kuopionkaupunginorkesteri_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    KuopionKaupunginorkesteriFiCrawler().run()


if __name__ == '__main__':
    main()
