import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://edgarmeyer.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendar/')
FEED_URL = f'{SOURCE_URL}?feed=gigpress'
SOURCE = 'Edgar Meyer'

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
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_value(soup, label):
    for item in soup.select('li'):
        strong = item.find('strong')
        if strong and clean_text(strong).rstrip(':').lower() == label.lower():
            strong.extract()
            return clean_text(item)
    return ''


def parse_time(value):
    match = re.search(r'\b(\d{1,2}):(\d{2})\s*([ap]m)\b', value, re.IGNORECASE)
    if not match:
        return None
    try:
        return datetime.strptime(''.join(match.groups()), '%I%M%p').strftime('%H:%M')
    except ValueError:
        return None


def event_url(soup, show_id):
    related = soup.find('a', string=re.compile(r'Related post', re.IGNORECASE))
    if related and related.get('href'):
        return related['href']
    tickets = soup.select_one('a.gigpress-tickets-link[href]')
    if tickets:
        return tickets['href']
    return f'{CALENDAR_URL}#show-{show_id}'


def related_description(session, soup):
    link = soup.find('a', string=re.compile(r'Related post', re.IGNORECASE))
    if not link or not link.get('href', '').startswith(SOURCE_URL):
        return None
    url = link['href']
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Edgar Meyer related event post',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None
    page = BeautifulSoup(response.text, 'html.parser')
    article = page.select_one('article') or page.select_one('main')
    return clean_text(article) or None


def parse_item(session, item):
    description_html = item.findtext('description') or ''
    soup = BeautifulSoup(description_html, 'html.parser')
    artist = labelled_value(soup, 'Artist')
    city = labelled_value(soup, 'City').split(',', 1)[0].strip()
    venue = labelled_value(soup, 'Venue')
    country_code = labelled_value(soup, 'Country').upper()
    guid = clean_text(item.findtext('guid'))
    show_match = re.search(r'#show-(\d+)', guid)

    try:
        event_date = parsedate_to_datetime(item.findtext('pubDate')).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None

    if not artist or not city or not venue or not re.fullmatch(r'[A-Z]{2}', country_code):
        return None
    if not show_match:
        return None

    return {
        'title': artist,
        'date': event_date,
        'url': event_url(soup, show_match.group(1)),
        'time_from': parse_time(labelled_value(soup, 'Time')),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': related_description(session, soup),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EdgarMeyerComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='edgarmeyer_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(FEED_URL, timeout=45)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except (requests.RequestException, ElementTree.ParseError) as error:
            log_message(
                'Failed to fetch Edgar Meyer GigPress feed',
                event='crawler_fetch_failed',
                level='error',
                url=FEED_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        records = []
        for item in root.findall('./channel/item'):
            record = parse_item(session, item)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    EdgarMeyerComCrawler().run()


if __name__ == '__main__':
    main()
