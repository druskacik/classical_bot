import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operalodz.com/'
CALENDAR_URL = urljoin(SOURCE_URL, 'Repertuar,17')
SOURCE = 'Teatr Wielki w Łodzi'
DEFAULT_CITY = 'Łódź'
DEFAULT_VENUE = 'Teatr Wielki w Łodzi'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response.text


def available_months(html):
    soup = BeautifulSoup(html, 'html.parser')
    return [
        option['value']
        for option in soup.select('#ym option[value]')
        if re.fullmatch(r'\d{4}-\d{2}', option['value'])
    ]


def parse_month_page(html, year_month):
    soup = BeautifulSoup(html, 'html.parser')
    year, month = (int(part) for part in year_month.split('-'))
    events = []
    for card in soup.select('#text_content .news_spacer.mar40'):
        title_link = card.select_one('#main_program_right a.col_text[href]')
        date_box = card.select_one('#main_program_left .mp_date')
        if not title_link or not date_box:
            continue

        date_match = re.search(r'\b(\d{1,2})\.(\d{2})\b', clean_text(date_box))
        time_match = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', clean_text(date_box))
        title = clean_text(title_link).rstrip('›').strip()
        if not date_match or not title:
            continue
        try:
            event_date = date(year, month, int(date_match.group(1))).isoformat()
        except ValueError:
            continue

        # The calendar is for the theatre's own stage. When a performance is
        # elsewhere, the site labels its place in the individual calendar card.
        card_text = clean_text(card)
        place_match = re.search(r'(?:^|\n)Miejsce:\s*([^\n]+)', card_text, re.I)
        venue = place_match.group(1).strip() if place_match else DEFAULT_VENUE
        city = DEFAULT_CITY
        postal_city = re.search(r'\b\d{2}-\d{3}\s+([^,\n]+)', venue)
        if postal_city:
            city = postal_city.group(1).strip()

        events.append({
            'title': title,
            'date': event_date,
            'url': urljoin(SOURCE_URL, title_link['href']),
            'time_from': f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None,
            'venue': venue,
            'city': city,
            'country_code': 'PL',
        })
    return events


def parse_detail_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#text_content')
    if not content:
        return None

    parts = []
    summary = content.select_one('.spectacle_left')
    description = content.select_one('#main_spectacle_1')
    for section in (summary, description):
        text = clean_text(section)
        if text:
            parts.append(text)

    # Concert, education, and special-event templates do not always use the
    # spectacle tabs. Preserve their main body after removing navigation and
    # ticket-only controls.
    if not parts:
        clone = BeautifulSoup(str(content), 'html.parser')
        for node in clone.select(
            '#sp_poster, .rep_dates, #buy_ticket_sp, #ul_spect, '
            '.spectacle_row_lab_pr, .spectacle_row_val_pr, script, style'
        ):
            node.decompose()
        fallback = clean_text(clone)
        if fallback:
            parts.append(fallback)
    return '\n\n'.join(parts) or None


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_html = get_page(session, CALENDAR_URL)
    months = available_months(first_html)
    if not months:
        raise ValueError('No repertoire months found')

    month_html = {}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_page, session, urljoin(SOURCE_URL, 'index.php'), {'id': 17, 'ym': month}): month
            for month in months
        }
        for future in as_completed(futures):
            month = futures[future]
            try:
                month_html[month] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape repertoire month', event='crawler_page_failed', level='warning',
                    url=f'{SOURCE_URL}index.php?id=17&ym={month}',
                    error_type=type(error).__name__, error_message=str(error),
                )

    events = []
    for month, html in month_html.items():
        events.extend(parse_month_page(html, month))
    unique_events = {
        (event['url'], event['date'], event['time_from'], event['venue']): event
        for event in events
    }

    descriptions = {}
    detail_urls = {event['url'] for event in unique_events.values()}
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_page, session, url): url for url in detail_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                descriptions[url] = parse_detail_page(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )

    records = []
    for event in unique_events.values():
        records.append({**event, 'description': descriptions.get(event['url'])})
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']))


class OperalodzComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operalodz_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    OperalodzComCrawler().run()


if __name__ == '__main__':
    main()
