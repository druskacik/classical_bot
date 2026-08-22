import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.robertcohen.info/'
DIARY_URL = urljoin(SOURCE_URL, 'diary')
SOURCE = 'Robert Cohen'

# Squarespace stores free-form addresses rather than normalized geography.  The
# artist tours internationally, so records need their own country code.
PLACE_RULES = (
    ('tampere', 'Tampere', 'FI'),
    ('finland', None, 'FI'),
    ('highgate', 'London', 'GB'),
    ('london', 'London', 'GB'),
    ('royal academy of music', 'London', 'GB'),
    ('nw1 5ht', 'London', 'GB'),
    ('1901 arts club', 'London', 'GB'),
    ('purcell school', 'Bushey', 'GB'),
    ('downing college', 'Cambridge', 'GB'),
    ('cambridge', 'Cambridge', 'GB'),
    ('orford church', 'Orford', 'GB'),
    ('hungerford', 'Hungerford', 'GB'),
    ('derbyshire', 'Derbyshire', 'GB'),
    ('united kingdom', None, 'GB'),
    ('suffolk', None, 'GB'),
    ('drogheda', 'Drogheda', 'IE'),
    ('ireland', None, 'IE'),
    ('nurmes', 'Nurmes', 'FI'),
    ('milano', 'Milan', 'IT'),
    ('milan', 'Milan', 'IT'),
    ('firenze', 'Florence', 'IT'),
    ('florence', 'Florence', 'IT'),
    ('italy', None, 'IT'),
    ('castelo branco', 'Castelo Branco', 'PT'),
    ('portugal', None, 'PT'),
    ('bratislava', 'Bratislava', 'SK'),
    ('cáceres', 'Cáceres', 'ES'),
    ('caceres', 'Cáceres', 'ES'),
    ('spain', None, 'ES'),
)


def clean_text(element):
    if element is None:
        return None
    text = element.get_text('\n', strip=True)
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip() or None


def infer_geography(text):
    normalized = text.casefold()
    city = None
    country_code = None
    for marker, rule_city, rule_country in PLACE_RULES:
        if marker in normalized:
            city = city or rule_city
            country_code = country_code or rule_country
    return city, country_code


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.eventitem')
    if article is None:
        return None

    title = clean_text(article.select_one('.eventitem-title'))
    date_node = article.select_one('.eventitem-meta-date .event-date')
    date = date_node.get('datetime', '').strip() if date_node else ''
    time_node = article.select_one('.eventitem-meta-time .event-time-24hr-start')
    time_from = clean_text(time_node)

    address = article.select_one('.eventitem-meta-address')
    address_lines = [clean_text(node) for node in address.select('.eventitem-meta-address-line')] if address else []
    address_lines = [line for line in address_lines if line]
    venue = address_lines[0] if address_lines else None

    body = article.select_one('.eventitem-column-content .sqs-layout')
    description = clean_text(body)
    evidence = ' '.join([title or '', description or '', *address_lines])
    city, country_code = infer_geography(evidence)

    # A location consisting only of the city is not a defensible venue.
    if venue and (
        (city and venue.casefold().strip(' ,.') == city.casefold())
        or venue.casefold().strip(' ,.') in {'highgate'}
    ):
        venue = None

    if not (title and re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) and venue and city and country_code):
        return None

    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def discover_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    return list(dict.fromkeys(
        urljoin(SOURCE_URL, link['href'])
        for link in soup.select('article.eventlist-event .eventlist-title-link[href]')
    ))


class RobertCohenInfoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='robertcohen_info',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; ClassicalBot/1.0)'
        log_message('Fetching Robert Cohen diary', event='crawler_url_fetch', url=DIARY_URL)
        response = session.get(DIARY_URL, timeout=45)
        response.raise_for_status()

        records = []
        for url in discover_urls(response.text):
            try:
                log_message('Fetching diary event', event='crawler_url_fetch', url=url)
                detail = session.get(url, timeout=45)
                detail.raise_for_status()
                record = parse_detail(detail.text, url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipping diary item without complete occurrence location',
                        event='crawler_event_skipped',
                        level='warning',
                        url=url,
                    )
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch diary event',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    RobertCohenInfoCrawler().run()


if __name__ == '__main__':
    main()
