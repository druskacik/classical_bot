import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.westwicklowfestival.com/'
EVENTS_URL = urljoin(SOURCE_URL, 'whats-on')
SOURCE = 'West Wicklow Chamber Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-IE,en;q=0.9',
}

DATE_TIME_RE = re.compile(
    r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+'
    r'(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\s+(\d{4})'
    r'(?:\s*,\s*(.+))?$',
    re.I,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def labelled_value(soup, label):
    for term in soup.select('dt'):
        if clean_text(term).casefold() == label.casefold():
            value = term.find_next_sibling('dd')
            return clean_text(value)
    return ''


def parse_time(value):
    text = clean_text(value).lower().replace('.', ':').replace(' ', '')
    if re.fullmatch(r'(?:12)?noon', text):
        return '12:00'
    if re.fullmatch(r'(?:12)?midnight', text):
        return '00:00'
    if re.fullmatch(r'\d{1,2}(?:am|pm)', text):
        text = re.sub(r'(?=am$|pm$)', ':00', text)
    for pattern in ('%I:%M%p', '%H:%M'):
        try:
            return datetime.strptime(text, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_date_time(value):
    match = DATE_TIME_RE.search(clean_text(value))
    if not match:
        return None, None
    try:
        event_date = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {match.group(3)}',
            '%d %B %Y',
        ).date().isoformat()
    except ValueError:
        return None, None
    return event_date, parse_time(match.group(4)) if match.group(4) else None


def infer_city(venue):
    venue = clean_text(venue)
    comma_parts = [part.strip() for part in venue.split(',') if part.strip()]
    if len(comma_parts) > 1 and comma_parts[-1].casefold() == 'blessington':
        return 'Blessington'
    if venue.casefold() == 'russborough house':
        return 'Blessington'
    return ''


def build_description(soup):
    parts = []
    for label in ('Artists', 'Programme'):
        value = labelled_value(soup, label)
        if value:
            parts.append(f'{label}\n{value}')

    for node in soup.select('main .intro .text-wrapper, main .content-block.block-text .text-wrapper'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(dict.fromkeys(parts)) or None


def parse_detail_page(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('main h1'))
    date_text = labelled_value(soup, 'Date & Time')
    event_date, time_from = parse_date_time(date_text)
    venue = labelled_value(soup, 'Venue')
    city = infer_city(venue)

    if not all((title, event_date, url, venue, city)):
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'IE',
        'description': build_description(soup),
    }


def extract_event_urls(html):
    soup = BeautifulSoup(html, 'html.parser')
    urls = []
    for link in soup.select('article.card-event a[href]'):
        url = urljoin(SOURCE_URL, link.get('href', ''))
        parsed = urlparse(url)
        if parsed.netloc == urlparse(SOURCE_URL).netloc and parsed.path.startswith('/whats-on/'):
            urls.append(url)
    return list(dict.fromkeys(urls))


class WestWicklowFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='westwicklowfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='IE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        response = session.get(EVENTS_URL, timeout=60)
        response.raise_for_status()

        records = []
        for url in extract_event_urls(response.text):
            try:
                detail_response = session.get(url, timeout=60)
                detail_response.raise_for_status()
                record = parse_detail_page(detail_response.text, url)
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch West Wicklow Festival event',
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
                    'Skipped incomplete West Wicklow Festival event',
                    event='crawler_item_skipped',
                    level='warning',
                    url=url,
                    error_type='IncompleteEventData',
                    error_message='Required title, date, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    WestWicklowFestivalComCrawler().run()


if __name__ == '__main__':
    main()
