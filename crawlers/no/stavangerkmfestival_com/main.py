import html
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.stavangerkmfestival.com/'
SOURCE = 'K&Mfest Stavanger'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}
LOCAL_TZ = ZoneInfo('Europe/Oslo')

# Older entries often put the actual venue in the title while leaving the
# Squarespace location title blank or set to the festival office.
KNOWN_VENUES = (
    ('sunde kirke', 'Sunde kirke', 'Stavanger'),
    ('tungenes fyr', 'Tungenes fyr', 'Randaberg'),
    ('ved fyret', 'Tungenes fyr', 'Randaberg'),
    ('utstein kloster', 'Utstein kloster', 'Mosterøy'),
    ('hummeren hotell', 'Hummeren Hotell', 'Tananger'),
    ('holmeegenes', 'Holmeegenes Museum', 'Stavanger'),
    ('sandnes kirke', 'Sandnes kirke', 'Sandnes'),
    ('sandnes kike', 'Sandnes kirke', 'Sandnes'),
    ('sola ruink', 'Sola ruinkirke', 'Sola'),
    ('st. petri', 'St. Petri kirke', 'Stavanger'),
    ('frimurerlogen', 'Frimurerlogen', 'Stavanger'),
    ('avaldsnes kirke', 'Avaldsnes kirke', 'Avaldsnes'),
    ('godtemplarnes hus', 'Godtemplarnes Hus', 'Stavanger'),
    ('kulturbruket 44/4', 'Kulturbruket 44/4', 'Bru'),
    ('bru kulturbruk', 'Kulturbruket 44/4', 'Bru'),
    ('villa tou', 'Villa Tou i Mølleparken', 'Tau'),
    ('mølleparken', 'Villa Tou i Mølleparken', 'Tau'),
    ('egersund kirke', 'Egersund kirke', 'Egersund'),
    ('st. svithun', 'St. Svithun kirke', 'Stavanger'),
    ('bergåstjern sykehjem', 'Bergåstjern sykehjem', 'Stavanger'),
    ('atelier kjell pahr-iversen', 'Atelier Kjell Pahr-Iversen', 'Stavanger'),
    ('atelieret kjell pahr-iversen', 'Atelier Kjell Pahr-Iversen', 'Stavanger'),
    ('ålgård kirke', 'Ålgård kirke', 'Ålgård'),
    ('bømessa sokndal', 'Bømessa Sokndal', 'Hauge i Dalane'),
    ('mjughøyden 9', 'Sunde kirke', 'Stavanger'),
    ('soknatun', 'Soknatun', 'Hauge i Dalane'),
    ('hillevåg kirke', 'Hillevåg kirke', 'Stavanger'),
    ('stavanger konserthus', 'Stavanger konserthus', 'Stavanger'),
)


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def body_text(value):
    if not value:
        return None
    soup = BeautifulSoup(value, 'html.parser')
    for element in soup.select('script, style, noscript, .sqs-block-button-container'):
        element.decompose()
    text = clean_text(soup.get_text('\n'))
    return text or None


def local_start(milliseconds):
    try:
        value = datetime.fromtimestamp(int(milliseconds) / 1000, tz=ZoneInfo('UTC'))
    except (TypeError, ValueError, OverflowError):
        return None
    value = value.astimezone(LOCAL_TZ)
    return value.date().isoformat(), value.strftime('%H:%M')


def inferred_place(event):
    location = event.get('location') or {}
    title = clean_text(event.get('title'))
    address_title = clean_text(location.get('addressTitle'))
    line1 = clean_text(location.get('addressLine1'))
    line2 = clean_text(location.get('addressLine2'))
    description = body_text(event.get('body')) or ''
    evidence = ' '.join((title, address_title, line1, line2, description)).casefold()

    for needle, venue, city in KNOWN_VENUES:
        if needle in evidence:
            return venue, city

    city = ''
    for value in (line2, line1):
        match = re.search(
            r'\b(Egersund|Stavanger|Hafrsfjord|Randaberg|Mosterøy|Tananger|'
            r'Sandnes|Sola|Avaldsnes|Bru|Tau|Rennesøy|Ålgård|Hauge i Dalane)\b',
            value,
            re.IGNORECASE,
        )
        if match:
            city = match.group(1)
            city = 'Stavanger' if city.casefold() == 'hafrsfjord' else city
            break

    # A location title is a venue only when it is not merely the festival
    # office or the locality itself.
    invalid_titles = {'', 'k&mfest stavanger', 'stavanger'}
    venue = address_title if address_title.casefold() not in invalid_titles else ''
    if venue and city:
        return venue, city
    return None


class StavangerKmFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='stavangerkmfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, url, **params):
        response = session.get(url, params=params, timeout=45)
        response.raise_for_status()
        return response

    def _collection_urls(self, session):
        response = self._get(session, SOURCE_URL)
        soup = BeautifulSoup(response.text, 'html.parser')
        urls = []
        for link in soup.select('a[href]'):
            label = clean_text(link.get_text(' '))
            if not re.fullmatch(r'Festival Concerts \d{4}', label, re.IGNORECASE):
                continue
            url = urljoin(SOURCE_URL, link.get('href'))
            if urlparse(url).netloc == urlparse(SOURCE_URL).netloc:
                urls.append(url.split('?', 1)[0])
        return list(dict.fromkeys(urls))

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []

        for collection_url in self._collection_urls(session):
            payload = self._get(session, collection_url, format='json').json()
            events = (payload.get('upcoming') or []) + (payload.get('past') or [])
            for event in events:
                title = clean_text(event.get('title'))
                occurrence = local_start(event.get('startDate'))
                place = inferred_place(event)
                path = event.get('fullUrl')
                if not title or not occurrence or not place or not path:
                    log_message(
                        'Skipping incomplete K&Mfest event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=urljoin(SOURCE_URL, path or collection_url),
                        error_type='IncompleteEvent',
                        error_message='Missing title, date, URL, venue, or city',
                    )
                    continue
                records.append({
                    'title': title,
                    'date': occurrence[0],
                    'url': urljoin(SOURCE_URL, path),
                    'time_from': occurrence[1],
                    'venue': place[0],
                    'city': place[1],
                    'description': body_text(event.get('body')),
                })

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'], item['title'], item['venue']
        ))


def main():
    return StavangerKmFestivalComCrawler().run()


if __name__ == '__main__':
    main()
