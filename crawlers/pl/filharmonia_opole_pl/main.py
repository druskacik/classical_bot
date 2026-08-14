import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonia.opole.pl/'
PROGRAM_URL = urljoin(SOURCE_URL, 'repertuar/?scope=all')
SOURCE = 'Filharmonia Opolska im. Józefa Elsnera'
DEFAULT_CITY = 'Opole'
DEFAULT_VENUE = 'Filharmonia Opolska im. Józefa Elsnera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4,
    'maja': 5, 'czerwca': 6, 'lipca': 7, 'sierpnia': 8,
    'września': 9, 'października': 10, 'listopada': 11, 'grudnia': 12,
}

CITY_FORMS = {
    'Bielsku-Białej': 'Bielsko-Biała',
    'Brzegu': 'Brzeg',
    'Katowicach': 'Katowice',
    'Kędzierzynie-Koźlu': 'Kędzierzyn-Koźle',
    'Krakowie': 'Kraków',
    'Nysie': 'Nysa',
    'Opolu': 'Opole',
    'Warszawie': 'Warszawa',
    'Wrocławiu': 'Wrocław',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def parse_date(text):
    match = re.search(r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})\b', text.lower())
    if not match:
        return None
    try:
        return date(int(match.group(3)), MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\|\s*(\d{1,2})\s*:\s*(\d{2})\b', text)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def page_count(html):
    soup = BeautifulSoup(html, 'html.parser')
    pages = [1]
    for link in soup.select('a[href*="pno="]'):
        match = re.search(r'[?&]pno=(\d+)', link.get('href', ''))
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def parse_index_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = []
    for title_tag in soup.select('main .nk-event-title'):
        card = title_tag.find_parent('div', class_='row')
        while card and not card.select_one('.nk-event-date'):
            card = card.find_parent('div', class_='row')
        link = title_tag.find_parent('a', href=True)
        if not card or not link or '/wydarzenia/' not in link['href']:
            continue
        date_text = clean_text(card.select_one('.nk-event-date'))
        event_date = parse_date(date_text)
        title = clean_text(title_tag)
        if title and event_date:
            events.append({
                'title': title,
                'date': event_date,
                'time_from': parse_time(date_text),
                'url': urljoin(SOURCE_URL, link['href']),
            })
    unique = {}
    for event in events:
        unique[(event['url'], event['date'], event['time_from'])] = event
    return list(unique.values())


def city_from_text(text):
    postal = re.search(r'\b\d{2}-\d{3}\s+([^\n,]+)', text)
    if postal:
        candidate = postal.group(1).strip(' .')
        if candidate and not re.search(r'\b(?:ul|al)\.', candidate, re.I):
            return candidate
    for form, city in CITY_FORMS.items():
        if re.search(r'\b' + re.escape(form) + r'\b', text, re.I):
            return city
    return None


def parse_detail_page(html, event):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article') or soup.select_one('main')
    if not article:
        return None, None

    content = article.select_one('.nk-event-content')
    description = clean_text(content) or None
    location_link = article.select_one('a[href*="/lokalizacje/"]')
    subtitle = clean_text(article.select_one('.nk-event-subtitle'))
    article_text = clean_text(article)
    date_text = clean_text(article.select_one('.nk-event-date'))
    detail_date = parse_date(date_text)
    if detail_date:
        event['date'] = detail_date
    event['time_from'] = parse_time(date_text)

    venue = clean_text(location_link) if location_link else ''
    if not venue:
        place = re.search(r'(?:^|\n)Miejsce:\s*([^\n]+)', article_text, re.I)
        venue = place.group(1).strip() if place else ''
    venue = re.sub(r'^Miejsce:\s*', '', venue, flags=re.I).strip()

    explicit_place_text = '\n'.join(value for value in (subtitle, article_text[:800]) if value)
    city = city_from_text(explicit_place_text)
    location_url = urljoin(SOURCE_URL, location_link['href']) if location_link else None

    # Events with no place information are performances in the institution's
    # own building. Touring events always carry a place in the subtitle/body.
    if not venue:
        venue = DEFAULT_VENUE
    if not city and (location_link or venue == DEFAULT_VENUE):
        city = DEFAULT_CITY

    if not venue or not city:
        return None, location_url
    return {
        **event,
        'venue': venue,
        'city': city,
        'country_code': 'PL',
        'description': description,
    }, location_url


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    first_html = get_page(session, PROGRAM_URL)
    total_pages = page_count(first_html)
    index_html = {1: first_html}

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(get_page, session, f'{PROGRAM_URL}&pno={number}'): number
            for number in range(2, total_pages + 1)
        }
        for future in as_completed(futures):
            number = futures[future]
            try:
                index_html[number] = future.result()
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape repertoire page', event='crawler_page_failed', level='warning',
                    url=f'{PROGRAM_URL}&pno={number}', error_type=type(error).__name__,
                    error_message=str(error),
                )

    events = []
    for html in index_html.values():
        events.extend(parse_index_page(html))
    events = list({(e['url'], e['date'], e['time_from']): e for e in events}.values())

    records = []
    def load_detail(event):
        try:
            record, _ = parse_detail_page(get_page(session, event['url']), event)
            return record
        except requests.RequestException as error:
            log_message(
                'Failed to scrape event detail', event='crawler_page_failed', level='warning',
                url=event['url'], error_type=type(error).__name__, error_message=str(error),
            )
            return None

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(load_detail, event) for event in events]
        for future in as_completed(futures):
            record = future.result()
            if record:
                records.append(record)

    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']))


class FilharmoniaOpolePlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_opole_pl',
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
    FilharmoniaOpolePlCrawler().run()


if __name__ == '__main__':
    main()
