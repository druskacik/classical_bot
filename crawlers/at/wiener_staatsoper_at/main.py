import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.wiener-staatsoper.at/'
CALENDAR_URL = urljoin(SOURCE_URL, 'kalender/')
SOURCE = 'Wiener Staatsoper'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-AT,de;q=0.9,en;q=0.7',
}

# First-party calendar genres whose concrete occurrences satisfy the project's
# inclusion guidance. Matinees explicitly advertise live musical/dance excerpts;
# general rehearsals are performances. Discussions, quizzes, workshops,
# symposiums and other outreach listings are deliberately excluded.
IN_SCOPE_GENRES = {
    'Ballett',
    'Ballettpremiere',
    'Generalprobe',
    'Kinderoper',
    'Konzert',
    'Matinee',
    'Oper',
    'Operette',
    'Opernpremiere',
    'Wiederaufnahme',
}


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            values = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(values, list):
            values = [values]
        for value in values:
            if isinstance(value, dict) and value.get('@type') == 'Event':
                return value
    return None


def parse_city(location):
    address = location.get('address') if isinstance(location, dict) else None
    if not isinstance(address, dict):
        return '', ''
    country_code = clean_text(address.get('addressCountry')).upper()
    locality = clean_text(address.get('addressLocality'))
    locality = re.sub(r',\s*(?:Austria|Österreich)$', '', locality, flags=re.I)
    if locality.casefold() == 'vienna':
        locality = 'Wien'
    return locality, country_code


def build_description(soup):
    parts = []
    for selector in (
        '.production-name',
        '.event-subtitle',
        '.production-cast',
        '.frame-type-theme_plot',
        '.bg-secondary.scroll-section .frame-type-text',
        '.frame-type-text .ce-bodytext',
        '.frame-type-textmedia .ce-bodytext',
    ):
        for element in soup.select(selector):
            text = clean_text(element)
            if text and text not in parts:
                parts.append(text)
    return '\n\n'.join(parts) or None


def parse_event(html, url, listing_genre='', listing_room=''):
    soup = BeautifulSoup(html, 'html.parser')
    event = parse_json_ld(soup)
    if not event:
        return None

    title = clean_text(event.get('name'))
    start_value = clean_text(event.get('startDate'))
    if not title or not start_value:
        return None
    try:
        start = datetime.fromisoformat(start_value)
    except ValueError:
        return None

    location = event.get('location') or {}
    city, country_code = parse_city(location)
    location_name = clean_text(location.get('name'))
    room_element = soup.select_one('.production-detail .event-room')
    room = clean_text(room_element) or clean_text(listing_room)
    venue = location_name
    if room and room.casefold() not in venue.casefold():
        venue = f'{venue} – {room}' if venue else room
    if not city or not country_code or not venue or venue.casefold() == city.casefold():
        return None

    genre_element = soup.select_one('.production-detail .genre-column')
    genre = clean_text(genre_element) or clean_text(listing_genre)
    description = build_description(soup)
    if genre:
        description = f'Genre: {genre}\n\n{description}' if description else f'Genre: {genre}'

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M'),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def extract_month_urls(html, base_url=CALENDAR_URL):
    soup = BeautifulSoup(html, 'html.parser')
    pattern = re.compile(r'/kalender/\d{4}/[^/]+/$')
    return sorted({
        canonical_url(urljoin(base_url, link['href']))
        for link in soup.select('a[href]')
        if pattern.search(urlsplit(urljoin(base_url, link['href'])).path)
    })


def extract_occurrences(html, month_url):
    soup = BeautifulSoup(html, 'html.parser')
    occurrences = []
    for item in soup.select('.event-list-item'):
        title = item.select_one('h2.event-title')
        genre_element = item.select_one('.event-genre')
        link = title.find_parent('a', href=True) if title else None
        genre = clean_text(genre_element)
        if not link or genre not in IN_SCOPE_GENRES:
            continue
        url = canonical_url(urljoin(month_url, link['href']))
        match = re.search(r'/(\d{4}-\d{2}-\d{2})/$', urlsplit(url).path)
        if not match:
            continue
        try:
            date.fromisoformat(match.group(1))
        except ValueError:
            continue
        occurrences.append({
            'url': url,
            'genre': genre,
            'room': clean_text(item.select_one('.event-room')),
        })
    return occurrences


class WienerStaatsoperCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='wiener_staatsoper_at',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='AT',
        upload_target='classical',
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        month_urls = extract_month_urls(response.text, response.url)
        if not month_urls and '/kalender/' in response.url:
            month_urls = [canonical_url(response.url)]

        occurrences = {}
        for month_url in month_urls:
            try:
                month_response = requests.get(month_url, headers=HEADERS, timeout=45)
                month_response.raise_for_status()
                for occurrence in extract_occurrences(month_response.text, month_url):
                    occurrences[occurrence['url']] = occurrence
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Wiener Staatsoper calendar month',
                    event='crawler_page_failed',
                    level='warning',
                    url=month_url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        records = []
        items = sorted(occurrences.values(), key=lambda item: item['url'])
        for offset in range(0, len(items), 12):
            batch = items[offset:offset + 12]
            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = {
                    executor.submit(requests.get, item['url'], headers=HEADERS, timeout=45): item
                    for item in batch
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        detail_response = future.result()
                        detail_response.raise_for_status()
                        record = parse_event(
                            detail_response.text,
                            item['url'],
                            item['genre'],
                            item['room'],
                        )
                        if record:
                            records.append(record)
                    except (requests.RequestException, ValueError) as error:
                        log_message(
                            'Failed to scrape Wiener Staatsoper event detail',
                            event='crawler_item_failed',
                            level='warning',
                            url=item['url'],
                            error_type=type(error).__name__,
                            error_message=str(error),
                        )

        records.sort(key=lambda item: (item['date'], item['time_from'], item['url']))
        return records


def main():
    WienerStaatsoperCrawler().run()


if __name__ == '__main__':
    main()
