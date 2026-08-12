import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.g-s-o.org.uk/'
RSS_URL = urljoin(SOURCE_URL, 'dbaction.php?action=rss&dbase=events')
PAST_EVENTS_URL = urljoin(SOURCE_URL, 'dbpage.php?pg=pastevents')
SOURCE = 'Guildford Symphony Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def parse_date(value, formats):
    for date_format in formats:
        try:
            return datetime.strptime(value.strip(), date_format).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', value, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(3).lower() == 'pm' and hour != 12:
        hour += 12
    elif match.group(3).lower() == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def table_value(soup, row_class):
    row = soup.select_one(f'tr.{row_class}')
    cells = row.select('td') if row else []
    return clean_text(cells[1]) if len(cells) > 1 else ''


def resolve_place(venue_text):
    compact = ' '.join(venue_text.split())
    lower = compact.lower()
    if 'london rd' in lower and 'guildford' in lower:
        return 'G Live', 'Guildford'
    if "queen eleanor's road" in lower and 'guildford' in lower:
        return "Queen Eleanor's School", 'Guildford'
    if 'charterhouse school' in lower and 'godalming' in lower:
        return 'Charterhouse School', 'Godalming'
    if "holy trinity & st mary's" in lower and 'guildford' in lower:
        return "Holy Trinity & St Mary's Guildford", 'Guildford'
    return None, None


def parse_detail(content, url, feed_title, feed_date):
    soup = BeautifulSoup(content, 'html.parser')
    page_title = clean_text(soup.title)
    title = feed_title

    date_match = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', page_title)
    event_date = (
        parse_date(date_match.group(1), ['%d/%m/%Y']) if date_match else feed_date
    )
    venue_text = table_value(soup, 'stdview_events_Venue')
    venue, city = resolve_place(venue_text)
    if not title or not event_date or not venue or not city:
        return None

    description_node = soup.select_one('td.event_description')
    description = clean_text(description_node) or None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(table_value(soup, 'stdview_events_Time')),
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def upcoming_items(content):
    soup = BeautifulSoup(content, 'xml')
    items = []
    for item in soup.select('item'):
        title = clean_text(item.title)
        url = clean_text(item.link)
        date = parse_date(clean_text(item.pubDate), ['%a, %d %b %Y %H:%M:%S %Z'])
        if title and url and date:
            items.append((url, title, date))
    return items


def archive_place(title, description):
    evidence = f'{title}\n{description}'.lower()
    if 'charterhouse' in evidence:
        return 'Charterhouse School', 'Godalming'
    if 'st john' in evidence and 'smith square' in evidence:
        return "St John's Smith Square", 'London'
    if 'g-live' in evidence or 'g-live' in evidence.replace(' ', ''):
        return 'G Live', 'Guildford'
    return None, None


def parse_archive(content):
    soup = BeautifulSoup(content, 'html.parser')
    records = []
    for index, heading in enumerate(soup.select('h2')):
        date_node = heading.find_next_sibling('div')
        if not date_node or 'font-style' not in (date_node.get('style') or ''):
            continue
        event_date = parse_date(clean_text(date_node), ['%a, %d %b %Y'])
        description_node = date_node.find_next_sibling('div')
        title = clean_text(heading)
        description = clean_text(description_node) or None
        venue, city = archive_place(title, description or '')
        if not title or not event_date or not venue or not city:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': f'{PAST_EVENTS_URL}#past-event-{index + 1}',
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': 'GB',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    items = upcoming_items(get_response(session, RSS_URL).content)
    records = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(get_response, session, url): (url, title, event_date)
            for url, title, event_date in items
        }
        for future in as_completed(futures):
            url, title, event_date = futures[future]
            try:
                record = parse_detail(future.result().content, url, title, event_date)
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Guildford Symphony Orchestra event detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    try:
        records.extend(parse_archive(get_response(session, PAST_EVENTS_URL).content))
    except requests.RequestException as error:
        log_message(
            'Failed to scrape Guildford Symphony Orchestra past events',
            event='crawler_archive_failed',
            level='warning',
            url=PAST_EVENTS_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class GSOOrgUkCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='g_s_o_org_uk',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    GSOOrgUkCrawler().run()


if __name__ == '__main__':
    main()
