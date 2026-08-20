import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://annaprohaska.com/'
SOURCE = 'Anna Prohaska'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}

# The artist's calendar tours internationally and its free-text locations do
# not have structured country fields. These stable venue/city clues cover the
# places used by the source and deliberately avoid applying a home-city default.
LOCATION_RULES = (
    ('barcelona', 'Barcelona', 'ES'),
    ('salzburger', 'Salzburg', 'AT'),
    ('helsinki', 'Helsinki', 'FI'),
    ('berlin', 'Berlin', 'DE'),
    ('hamburg', 'Hamburg', 'DE'),
    ('kronberg', 'Kronberg im Taunus', 'DE'),
    ('ascona', 'Ascona', 'CH'),
    ('festspiele erl', 'Erl', 'AT'),
    ('ulm', 'Ulm', 'DE'),
    ('oxford', 'Oxford', 'GB'),
    ('kraków', 'Kraków', 'PL'),
    ('krakow', 'Kraków', 'PL'),
    ('bari', 'Bari', 'IT'),
    ('kiel', 'Kiel', 'DE'),
    ('frankfurt', 'Frankfurt am Main', 'DE'),
    ('florence', 'Florence', 'IT'),
    ('firenze', 'Florence', 'IT'),
    ('reggio emilia', 'Reggio Emilia', 'IT'),
    ('monheim', 'Monheim am Rhein', 'DE'),
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value, month_heading):
    match = re.search(r'(\d{4})', clean_text(month_heading))
    value = re.sub(r'^[^,]+,\s*', '', clean_text(value))
    if not match or not value:
        return ''
    try:
        return datetime.strptime(f'{value} {match.group(1)}', '%d %B %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%H:%M'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_location(value):
    location = clean_text(value)
    folded = location.casefold()
    for clue, city, country_code in LOCATION_RULES:
        if clue in folded:
            # Keep the first-party location verbatim as the venue: it often
            # combines a festival/complex and its named hall, all useful data.
            if location.casefold() == city.casefold():
                return '', '', ''
            return location, city, country_code
    return '', '', ''


def parse_calendar(html):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for grid in soup.select('h3 + .grid-event'):
        heading = grid.find_previous_sibling('h3')
        items = grid.select(':scope > .item')
        for index in range(0, len(items) - 1, 2):
            schedule, details = items[index:index + 2]
            title = clean_text(details.select_one('.title'))
            event_date = parse_date(schedule.select_one('.date'), heading)
            venue, city, country_code = parse_location(details.select_one('.location'))
            link = details.select_one('.link a[href]')
            url = urljoin(SOURCE_URL, link.get('href', '').strip()) if link else ''
            if not all((title, event_date, url, venue, city, country_code)):
                log_message(
                    'Skipped incomplete Anna Prohaska calendar event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url or SOURCE_URL,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, city, or country is missing',
                )
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(schedule.select_one('.time')),
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class AnnaProhaskaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='annaprohaska_com',
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
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        # The document is UTF-8 but its response currently omits a charset,
        # causing requests to otherwise assume ISO-8859-1.
        response.encoding = 'utf-8'
        records = parse_calendar(response.text)
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    AnnaProhaskaComCrawler().run()


if __name__ == '__main__':
    main()
