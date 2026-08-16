import html
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://cheyennesymphony.org/'
EVENTS_API_URL = f'{SOURCE_URL}wp-json/wp/v2/events'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
SOURCE = 'Cheyenne Symphony Orchestra'
CITY = 'Cheyenne'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

DATE_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),\s+(\d{4})\b',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b', re.IGNORECASE)


def clean_text(value):
    if not value:
        return ''
    text = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text):
    match = DATE_RE.search(text or '')
    if not match:
        return ''
    try:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(text):
    match = TIME_RE.search(text or '')
    if not match:
        return None
    hour, minute, meridiem = match.groups()
    hour = int(hour) % 12 + (12 if meridiem.lower() == 'p' else 0)
    return f'{hour:02d}:{int(minute or 0):02d}'


def labeled_value(soup, label):
    heading = soup.find(
        ['h2', 'h3', 'h4', 'h5'],
        string=lambda value: clean_text(value).lower() == label.lower() if value else False,
    )
    if not heading:
        return ''

    module = heading.find_parent(class_=re.compile(r'\bet_pb_module\b')) or heading.parent
    for node in module.find_all_next(class_='et_pb_text_inner', limit=4):
        value = clean_text(node.get_text('\n', strip=True))
        if value and value.lower() not in {'tickets information', 'length'}:
            return value
    return ''


def parse_event_page(page_html, url):
    soup = BeautifulSoup(page_html, 'html.parser')
    title_node = soup.find('h1')
    title = clean_text(title_node.get_text(' ', strip=True) if title_node else '')

    body = soup.select_one('.et-l--body') or soup.find('main') or soup.body
    body_text = clean_text(body.get_text('\n', strip=True) if body else '')
    event_date = parse_date(body_text)
    time_from = parse_time(body_text)
    venue = labeled_value(soup, 'Venue')

    if not title or not event_date or not venue:
        log_message(
            'Skipping event with incomplete required fields',
            event='crawler_event_skipped',
            level='warning',
            url=url,
            has_title=bool(title),
            has_date=bool(event_date),
            has_venue=bool(venue),
        )
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': CITY,
        'country_code': 'US',
        'description': body_text or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def calendar_cards(session):
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    page_html = response.text
    soup = BeautifulSoup(page_html, 'html.parser')
    cards = list(soup.select('article.df-cpt-item.type-events'))

    settings_match = re.search(r'var df_cpt_filter\s*=\s*({.*?});', page_html, re.DOTALL)
    nonce_match = re.search(r'"et_frontend_nonce":"([^"]+)"', page_html)
    load_more = soup.select_one('a.df-cptfilter-load-more')
    if not settings_match or not nonce_match or not load_more:
        return cards

    settings = next(iter(json.loads(settings_match.group(1)).values()))
    page_count = int(load_more.get('data-pages', '1'))
    payload_keys = {
        'post_type', 'post_display', 'posts_number', 'offset_number', 'equal_height',
        'use_image_as_background', 'use_background_scale', 'cpt_item_inner',
        'cpt_item_outer', 'load_more', 'use_load_more_icon', 'load_more_font_icon',
        'load_more_icon_pos', 'use_load_more_text', 'use_empty_post_message',
        'empty_post_message', 'all_items', 'multi_filter_type', 'orderby',
        'enable_acf_filter', 'enable_pod_filter', 'entire_item_clickable',
    }
    payload = {key: settings.get(key, '') for key in payload_keys}
    payload.update({
        'et_frontend_nonce': nonce_match.group(1),
        'action': 'df_cpt_filter_data',
        'term_id': load_more.get('data-term', ''),
        'selected_tax': json.dumps(settings.get('selected_tax', 'category')),
        'selected_acf': '[]',
        'selected_pod': '[]',
        'selected_author': '[""]',
        'search_value': '',
        '_request': 'loadmore',
        'excluded_post_ids': '',
    })

    for current_page in range(1, page_count):
        payload['current_page'] = str(current_page)
        ajax = session.post(f'{SOURCE_URL}wp-admin/admin-ajax.php', data=payload, timeout=45)
        ajax.raise_for_status()
        fragment = BeautifulSoup(ajax.json().get('data', ''), 'html.parser')
        cards.extend(fragment.select('article.df-cpt-item.type-events'))
    return cards


def card_data(card):
    title_link = card.select_one('.df-cpt-title a[href]')
    date_node = card.select_one('.difl_cptitem_3 .df-acf-field-inner')
    venue_node = card.select_one('.difl_cptitem_5 .df-acf-field-inner')
    if not title_link or not date_node or not venue_node:
        return None
    return {
        'title': clean_text(title_link.get_text(' ', strip=True)),
        'date': parse_date(clean_text(date_node.get_text(' ', strip=True))),
        'url': title_link.get('href', ''),
        'venue': clean_text(venue_node.get_text(' ', strip=True)),
    }


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []

    for card in calendar_cards(session):
        listing = card_data(card)
        if not listing or not all(listing.values()):
            continue
        url = listing['url']
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            body = soup.select_one('.et-l--body') or soup.find('main') or soup.body
            body_text = clean_text(body.get_text('\n', strip=True) if body else '')
            records.append({
                **listing,
                'time_from': parse_time(body_text),
                'city': CITY,
                'country_code': 'US',
                'description': body_text or None,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        except requests.RequestException as error:
            log_message(
                'Failed to fetch event page',
                event='crawler_event_fetch_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )

    return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


class CheyenneSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='cheyennesymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    CheyenneSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
