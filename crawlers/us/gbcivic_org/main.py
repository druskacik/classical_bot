import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://gbcivic.org/'
SOURCE = 'Civic Symphony of Green Bay'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
CONCERT_CATEGORY_ID = 8

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_datetime(value):
    match = re.search(
        r'DATE:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*'
        r'TIME:\s*(\d{1,2}:\d{2}\s*[ap]m)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(match.group(1), '%B %d, %Y').date().isoformat()
        time_from = datetime.strptime(match.group(2).replace(' ', '').upper(), '%I:%M%p')
    except ValueError:
        return None, None
    return event_date, time_from.strftime('%H:%M')


def parse_location(description):
    paragraph = next(
        (
            item for item in description.find_all('p')
            if re.search(r'^LOCATION\s*:', clean_text(item), re.IGNORECASE)
        ),
        None,
    )
    if paragraph is None:
        return '', ''

    address_link = paragraph.find('a', href=re.compile(r'(?:maps|google)', re.IGNORECASE))
    address = clean_text(address_link)
    city_match = re.search(r',\s*([^,]+),\s*[A-Z]{2}(?:\s+\d{5})?\s*$', address)
    city = city_match.group(1).strip() if city_match else ''

    location = BeautifulSoup(str(paragraph), 'html.parser')
    for node in location.find_all(['strong', 'a']):
        node.decompose()
    venue = clean_text(location).strip(' ,-')
    return venue, city


def event_description(description):
    content = BeautifulSoup(str(description), 'html.parser')
    for node in content.select('script, style, form, .su-button, img'):
        node.decompose()
    for paragraph in content.find_all(['p', 'em']):
        text = clean_text(paragraph)
        if (
            re.search(r'^LOCATION\s*:', text, re.IGNORECASE)
            or re.search(r'^(Order Tickets|Ticket Package|Purchase (?:individual )?tickets)', text, re.IGNORECASE)
            or re.search(r'^For help with ticket orders', text, re.IGNORECASE)
        ):
            paragraph.decompose()
    text = clean_text(content)
    return text or None


def parse_event(page, url):
    soup = BeautifulSoup(page, 'html.parser')
    article = soup.select_one('article.events, article.type-events')
    if article is None:
        return None
    title = clean_text(article.select_one('h1, h2, h3'))
    event_date, time_from = parse_datetime(clean_text(article.select_one('.event-time')))
    description_node = article.select_one('.event-description')
    venue, city = parse_location(description_node) if description_node else ('', '')
    if not all((title, event_date, url, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': event_description(description_node),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class GbCivicOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gbcivic_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        events = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            response = session.get(
                API_URL,
                params={
                    'event-categories': CONCERT_CATEGORY_ID,
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'asc',
                    '_fields': 'id,link,title,event-categories',
                },
                timeout=45,
            )
            response.raise_for_status()
            events.extend(response.json())
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            page += 1

        records = []
        for event in events:
            url = clean_text(event.get('link'))
            if not url:
                continue
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                record = parse_event(response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Civic Symphony of Green Bay concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Civic Symphony of Green Bay concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    GbCivicOrgCrawler().run()


if __name__ == '__main__':
    main()
