import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.thirdcoastchambercollective.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'events')
SOURCE = 'Third Coast Chamber Collective'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
}

# A few entries omit the city because the venue name is locally unambiguous.
# Other touring events retain the city printed in their own location line.
VENUE_CITIES = {
    'Nordic Center Duluth': 'Duluth',
    'REIF CENTER, Ives Theatre': 'Grand Rapids',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = clean_text(value).upper().replace('.', '')
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def location_from_paragraphs(paragraphs):
    if not paragraphs:
        return '', '', 0

    location = paragraphs[0]
    used = 1
    # One event splits ", Duluth, MN" into its own paragraph.
    if len(paragraphs) > 1 and re.match(r'^,?\s*[A-Za-z .\'’-]+,\s*MN\b', paragraphs[1]):
        location += paragraphs[1]
        used = 2

    city_match = re.search(
        r'\b(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Drive|Dr)\.?\s+'
        r'([A-Za-z][A-Za-z .\'’-]*),\s*MN\b\.?',
        location,
    )
    if not city_match:
        city_match = re.search(r',\s*([A-Za-z][A-Za-z .\'’-]*),\s*MN\b\.?', location)
    city = clean_text(city_match.group(1)) if city_match else VENUE_CITIES.get(location, '')
    if not city:
        return '', '', used

    if city_match:
        venue_part = location[:city_match.start()].strip(' ,')
    else:
        venue_part = location

    # Strip a street address when it follows the venue name.
    address_match = re.search(r',\s*\d+\s+', venue_part)
    if address_match:
        venue_part = venue_part[:address_match.start()]
    venue = clean_text(venue_part).strip(' ,')
    return venue, city, used


def make_record(item):
    title_element = item.select_one('.eventlist-title-link')
    date_element = item.select_one('time.event-date[datetime]')
    if not title_element or not date_element:
        return None

    title = clean_text(title_element.get_text(' ', strip=True))
    url = urljoin(SOURCE_URL, title_element.get('href', ''))
    date_value = clean_text(date_element.get('datetime'))
    try:
        date = datetime.strptime(date_value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None

    paragraphs = [
        clean_text(paragraph.get_text(' ', strip=True))
        for paragraph in item.select('.eventlist-excerpt p')
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    venue, city, location_paragraphs = location_from_paragraphs(paragraphs)
    if not title or not url or not venue or not city:
        return None

    time_element = item.select_one('.event-time-localized-start')
    description = '\n\n'.join(paragraphs[location_paragraphs:]).strip()
    return {
        'title': title,
        'date': date,
        'url': url,
        'time_from': parse_time(time_element.get_text(' ', strip=True)) if time_element else None,
        'venue': venue,
        'city': city,
        'description': description or None,
    }


def scrape_concerts():
    response = requests.get(EVENTS_URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    items = soup.select('.eventlist-event')
    if not items:
        raise RuntimeError('No event entries found on the Squarespace events page')

    records = []
    for item in items:
        record = make_record(item)
        if record:
            records.append(record)
        else:
            title_element = item.select_one('.eventlist-title-link')
            log_message(
                'Skipped event with incomplete required fields',
                event='crawler_item_skipped',
                level='warning',
                url=urljoin(SOURCE_URL, title_element.get('href', '')) if title_element else EVENTS_URL,
            )
    return sorted(records, key=lambda record: (record['date'], record['time_from'] or '', record['title']))


class ThirdCoastChamberCollectiveComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='thirdcoastchambercollective_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=['title', 'date', 'url', 'time_from', 'venue', 'city', 'description'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    ThirdCoastChamberCollectiveComCrawler().run()


if __name__ == '__main__':
    main()
