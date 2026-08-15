import base64
import json
import re
from datetime import datetime
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://abiphil.com/'
SOURCE = 'Abilene Philharmonic'
CONCERTS_URL = f'{SOURCE_URL}current-concerts/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def clean_text(value):
    if not value:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def popup_id_from_href(href):
    decoded = unquote(href or '')
    match = re.search(r'settings=([^&]+)', decoded)
    if not match:
        return None
    try:
        encoded = match.group(1)
        encoded += '=' * (-len(encoded) % 4)
        settings = json.loads(base64.b64decode(encoded).decode('utf-8'))
        return str(settings['id'])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def parse_date(value):
    value = re.sub(r'(?<=\d)(?:st|nd|rd|th)', '', clean_text(value), flags=re.I)
    try:
        return datetime.strptime(value, '%B %d, %Y').date().isoformat()
    except ValueError:
        return ''


def parse_time(value):
    value = clean_text(value).replace('.', '').upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def parse_popup(popup, popup_id):
    lines = [clean_text(line) for line in popup.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    headings = [clean_text(item.get_text(' ', strip=True)) for item in popup.select('h1, h2, h3')]
    headings = [item for item in headings if item and item.upper() != 'PURCHASE TICKETS']
    title = headings[1] if len(headings) > 1 else (headings[0] if headings else '')

    date_index = None
    event_date = ''
    for index, line in enumerate(lines):
        if re.fullmatch(r'[A-Za-z]+ \d{1,2}(?:st|nd|rd|th)?, \d{4}', line, re.I):
            event_date = parse_date(line)
            date_index = index
            break

    time_from = None
    venue = ''
    city = 'Abilene'
    if date_index is not None:
        for index in range(date_index + 1, min(date_index + 4, len(lines))):
            candidate_time = parse_time(lines[index])
            if candidate_time:
                time_from = candidate_time
                if index + 1 < len(lines):
                    location = lines[index + 1]
                    parts = re.split(r'\s*[\u2022|]\s*', location, maxsplit=1)
                    venue = clean_text(parts[0])
                    if len(parts) > 1:
                        parsed_city = clean_text(parts[1].split(',')[0])
                        if parsed_city:
                            city = parsed_city
                break

    purchase_link = next(
        (
            link for link in popup.select('a[href]')
            if re.search(r'PURCHASE TICKETS', link.get_text(' ', strip=True), re.I)
        ),
        None,
    )
    url = clean_text(purchase_link.get('href')) if purchase_link else ''
    if not url:
        url = f'{CONCERTS_URL}#concert-{popup_id}'

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
        'description': '\n'.join(lines) or None,
        'source_url': CONCERTS_URL,
        'source': SOURCE,
    }


class AbiphilComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='abiphil_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        page = soup.select_one('[data-elementor-type="wp-post"]')
        popup_ids = []
        if page:
            for link in page.select('a[href*="elementor-action"]'):
                popup_id = popup_id_from_href(link.get('href'))
                if popup_id and popup_id not in popup_ids:
                    popup_ids.append(popup_id)

        records = []
        for popup_id in popup_ids:
            popup = soup.select_one(
                f'[data-elementor-type="popup"][data-elementor-id="{popup_id}"]'
            )
            record = parse_popup(popup, popup_id) if popup else None
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Abilene Philharmonic concert',
                    event='crawler_item_skipped',
                    level='warning',
                    url=f'{CONCERTS_URL}#concert-{popup_id}',
                    error_type='IncompleteEventData',
                    error_message='Required title, date, URL, venue, or city is missing',
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    AbiphilComCrawler().run()


if __name__ == '__main__':
    main()
