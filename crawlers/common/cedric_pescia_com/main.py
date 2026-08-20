import re
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.cedric-pescia.com/fr/'
EVENTS_URL = urljoin(SOURCE_URL, 'concerts.html')
SOURCE = 'Cédric Pescia'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9,en;q=0.8',
}

# The artist tours internationally and the calendar does not have a country
# field. Prefer explicit country text, then conservative city/domain evidence.
COUNTRY_NAMES = {
    'suisse': 'CH', 'switzerland': 'CH', 'schweiz': 'CH',
    'france': 'FR', 'allemagne': 'DE', 'germany': 'DE', 'deutschland': 'DE',
    'autriche': 'AT', 'austria': 'AT', 'italie': 'IT', 'italy': 'IT',
    'belgique': 'BE', 'belgium': 'BE', 'espagne': 'ES', 'spain': 'ES',
    'royaume-uni': 'GB', 'united kingdom': 'GB', 'pays-bas': 'NL',
    'netherlands': 'NL', 'états-unis': 'US', 'united states': 'US',
}
CITY_COUNTRIES = {
    'pully': 'CH', 'lausanne': 'CH', 'genève': 'CH', 'geneva': 'CH',
    'berne': 'CH', 'bern': 'CH', 'zurich': 'CH', 'bâle': 'CH', 'basel': 'CH',
    'neuchâtel': 'CH', 'vevey': 'CH', 'lucerne': 'CH',
    'paris': 'FR', 'lyon': 'FR', 'strasbourg': 'FR',
    'berlin': 'DE', 'hamburg': 'DE', 'munich': 'DE', 'münchen': 'DE',
    'vienne': 'AT', 'vienna': 'AT', 'wien': 'AT', 'bruxelles': 'BE',
    'brussels': 'BE', 'london': 'GB', 'londres': 'GB',
}
DOMAIN_COUNTRIES = {
    'ch': 'CH', 'fr': 'FR', 'de': 'DE', 'at': 'AT', 'it': 'IT',
    'be': 'BE', 'nl': 'NL', 'es': 'ES', 'uk': 'GB',
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def country_for(text, city, detail_url):
    folded = clean_text(text).casefold()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf'(?<!\w){re.escape(name)}(?!\w)', folded):
            return code
    code = CITY_COUNTRIES.get(clean_text(city).casefold())
    if code:
        return code
    hostname = (urlparse(detail_url).hostname or '').lower()
    return DOMAIN_COUNTRIES.get(hostname.rsplit('.', 1)[-1])


def split_venue_city(value):
    value = clean_text(value)
    if ',' not in value:
        return value, ''
    venue, city = value.rsplit(',', 1)
    return clean_text(venue), clean_text(city)


def parse_event(node):
    time_node = node.select_one('time[datetime]')
    heading = node.select_one('h3')
    venue_node = node.select_one('h4')
    link = node.select_one('a[href]')
    if not all((time_node, heading, venue_node)):
        return None

    event_date = clean_text(time_node.get('datetime'))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_date):
        return None

    venue, city = split_venue_city(venue_node.get_text(' ', strip=True))
    try:
        date.fromisoformat(event_date)
    except ValueError:
        return None

    detail_url = urljoin(EVENTS_URL, link.get('href')) if link else EVENTS_URL

    middle = heading.parent
    lines = [clean_text(item) for item in middle.stripped_strings]
    lines = [item for item in lines if item]
    heading_text = clean_text(heading.get_text(' ', strip=True))
    details = [item for item in lines if item != heading_text]
    title = details[0] if details else heading_text

    venue_parent = venue_node.parent
    venue_lines = [clean_text(item) for item in venue_parent.stripped_strings]
    time_from = None
    for item in venue_lines:
        match = re.fullmatch(r'(\d{1,2})[h:.](\d{2})', item)
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            time_from = f'{int(match.group(1)):02d}:{match.group(2)}'
            break

    context = ' '.join(lines + venue_lines)
    country_code = country_for(context, city, detail_url)
    if not all((title, venue, city, country_code)):
        return None

    description_parts = [heading_text, *details]
    description = '\n'.join(dict.fromkeys(description_parts)) or None
    return {
        'title': title,
        'date': event_date,
        'url': detail_url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class CedricPesciaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cedric_pescia_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(EVENTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for node in soup.select('.agenda .date'):
            record = parse_event(node)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Cédric Pescia concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=EVENTS_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, venue, city, or country is missing',
                )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    CedricPesciaComCrawler().run()


if __name__ == '__main__':
    main()
