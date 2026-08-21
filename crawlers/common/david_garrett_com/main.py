import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'David Garrett'
SOURCE_URL = 'https://www.david-garrett.com/en/'
EVENTS_URL = f'{SOURCE_URL}live'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# The calendar does not publish a country field. Its event titles do publish
# the city, so known tour stops can be resolved without guessing from venues.
CITY_COUNTRIES = {
    'Almaty': 'KZ',
    'Astana': 'KZ',
    'Bucharest': 'RO',
    'Istanbul': 'TR',
    'München': 'DE',
    'Munich': 'DE',
    'Salzburg': 'AT',
    'Sofia': 'BG',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def city_from_title(title):
    match = re.search(r'\s+in\s+([^–—|,]+?)\s*$', title, re.IGNORECASE)
    return clean_text(match.group(1)) if match else ''


def parse_event(element):
    title = clean_text(element.select_one('[itemprop="name"].title'))
    time_element = element.select_one('time[itemprop="startDate"]')
    raw_start = clean_text(time_element.get('datetime')) if time_element else ''
    try:
        start = datetime.fromisoformat(raw_start)
    except ValueError:
        return None

    city = city_from_title(title)
    country_code = CITY_COUNTRIES.get(city, '')
    location = clean_text(element.select_one('[itemprop="location"] [itemprop="name"]'))
    venue = re.sub(
        r',\s*(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:am|pm)?\s*$',
        '',
        location,
        flags=re.IGNORECASE,
    ).strip()

    ticket = element.select_one('.event-actions a.tickets[href]')
    url = clean_text(ticket.get('href')) if ticket else ''

    description_parts = []
    # Select only the desktop details; the page repeats the same content in a
    # separate mobile block.
    details = element.select_one('.event-info > .details')
    if details:
        for section in details.select('.partner, .program'):
            heading = clean_text(section.select_one('h4'))
            body = clean_text(section.select_one('.text'))
            part = f'{heading}\n{body}' if heading and body else body
            if part and part not in description_parts:
                description_parts.append(part)

    required = (title, city, country_code, venue, url)
    if not all(required):
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': '\n\n'.join(description_parts) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class DavidGarrettComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='david_garrett_com',
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
        for element in soup.select('.mod_eventlist .event[itemtype="http://schema.org/Event"]'):
            record = parse_event(element)
            if record:
                records.append(record)
            else:
                title = clean_text(element.select_one('[itemprop="name"].title'))
                log_message(
                    'Skipped incomplete David Garrett event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=EVENTS_URL,
                    error_type='IncompleteEventData',
                    error_message=f'Required event data or country mapping missing for {title!r}',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    DavidGarrettComCrawler().run()


if __name__ == '__main__':
    main()
