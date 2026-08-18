import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musiciensdulouvre.fr/'
SEASON_URL = urljoin(SOURCE_URL, 'saison/')
PAST_SEASON_URL = f'{SEASON_URL}?lmdl_saison_past=1'
API_URL = urljoin(SOURCE_URL, 'wp-json/wp/v2/lmdl_event')
SOURCE = 'Les Musiciens du Louvre'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'jan': 1, 'janv': 1, 'janvier': 1,
    'fev': 2, 'fevr': 2, 'fevrier': 2,
    'mar': 3, 'mars': 3,
    'avr': 4, 'avril': 4,
    'mai': 5,
    'jun': 6, 'juin': 6,
    'jul': 7, 'juil': 7, 'juillet': 7,
    'aou': 8, 'aout': 8,
    'sep': 9, 'sept': 9, 'septembre': 9,
    'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11,
    'dec': 12, 'decembre': 12,
}

# This is a touring orchestra. Countries are inferred only from cities printed
# on its season cards; the ensemble's Grenoble address is never used as an
# event location default.
CITY_COUNTRIES = {
    'barcelona': 'ES',
    'barcelone': 'ES',
    'geneve': 'CH',
    'lausanne': 'CH',
    'lisbonne': 'PT',
    'madrid': 'ES',
    'milan': 'IT',
    'sion': 'CH',
    'turin': 'IT',
    'udine': 'IT',
    'zurich': 'CH',
}

COUNTRY_CODES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE', 'chine': 'CN',
    'coree du sud': 'KR', 'espagne': 'ES', 'france': 'FR', 'hongrie': 'HU',
    'italie': 'IT', 'japon': 'JP', 'mexique': 'MX', 'pays-bas': 'NL',
    'pologne': 'PL', 'portugal': 'PT', 'republique tcheque': 'CZ',
    'royaume-uni': 'GB', 'russie': 'RU', 'suede': 'SE', 'suisse': 'CH',
    'thailande': 'TH', 'turquie': 'TR',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    import unicodedata

    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, clean_text(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_card_date(article):
    time_element = article.select_one('time[datetime]')
    if time_element:
        candidate = clean_text(time_element.get('datetime'))[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass

    text = clean_text(article)
    numeric = re.search(r'\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b', text)
    if numeric:
        try:
            return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1))).isoformat()
        except ValueError:
            return None

    words = re.search(
        r'\b(JAN(?:V(?:IER)?)?|F[ÉE]V(?:R(?:IER)?)?|MAR(?:S)?|AVR(?:IL)?|MAI|'
        r'JUIN?|JUIL(?:LET)?|AO[ÛU]T|SEPT(?:EMBRE)?|OCT(?:OBRE)?|NOV(?:EMBRE)?|'
        r'D[ÉE]C(?:EMBRE)?)\s+(\d{1,2})\s+(20\d{2})\b',
        text,
        re.I,
    )
    if not words:
        return None
    month = MONTHS.get(normalized(words.group(1)).rstrip('.')[:4])
    if month is None:
        month = MONTHS.get(normalized(words.group(1)).rstrip('.'))
    try:
        return date(int(words.group(3)), month, int(words.group(2))).isoformat()
    except (TypeError, ValueError):
        return None


def parse_times(article):
    value = clean_text(article)
    match = re.search(
        r'\b([01]?\d|2[0-3])[:h](\d{2})(?:\s*[–—-]\s*([01]?\d|2[0-3])[:h](\d{2}))?',
        value,
    )
    if not match:
        return None, None
    start = f'{int(match.group(1)):02d}:{match.group(2)}'
    end = f'{int(match.group(3)):02d}:{match.group(4)}' if match.group(3) else None
    return start, end


def parse_location(article):
    candidates = []
    for element in article.select('p'):
        text = clean_text(element)
        if '—' in text or re.search(r'\s+-\s+', text):
            candidates.append(text)
    for text in candidates:
        text = re.sub(r'\s+\d{1,2}[:h]\d{2}.*$', '', text).strip()
        parts = re.split(r'\s+[—–]\s+|\s+-\s+', text)
        if len(parts) >= 2:
            venue = ' — '.join(parts[:-1]).strip()
            city = parts[-1].strip()
            if venue and city and not re.search(r'\d{1,2}[:h]\d{2}', city):
                return venue, city
    return '', ''


def country_for_city(city):
    return CITY_COUNTRIES.get(normalized(city), 'FR')


def parse_card(article):
    detail_link = article.select_one('a[href*="/event/"]')
    heading = article.select_one('h2, h3, h4')
    title = clean_text(heading)
    url = canonical_url(detail_link.get('href')) if detail_link else ''
    event_date = parse_card_date(article)
    time_from, time_to = parse_times(article)
    venue, city = parse_location(article)
    if not title or not url or not event_date or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'time_to': time_to,
        'venue': venue,
        'city': city,
        'country_code': country_for_city(city),
        'description': None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_season(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for article in soup.select('main article'):
        if not article.select_one('a[href*="/event/"]'):
            continue
        record = parse_card(article)
        if record:
            records.append(record)
        else:
            link = article.select_one('a[href*="/event/"]')
            log_message(
                'Skipped incomplete Les Musiciens du Louvre season entry',
                event='crawler_item_skipped',
                level='warning',
                url=canonical_url(link.get('href')) if link else page_url,
                error_type='IncompleteEventData',
                error_message='Required title, date, URL, venue, or city is missing',
            )
    return records


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    main = soup.select_one('main')
    if not main:
        return None
    for element in main.select(
        'script, style, nav, form, button, .elementor-button-wrapper, '
        '.sharedaddy, .post-navigation, .event-tickets'
    ):
        element.decompose()
    parts = []
    for element in main.select('h2, h3, h4, p, li'):
        if element.find_parent(['li', 'p']):
            continue
        text = clean_text(element)
        if text and text not in parts and not re.fullmatch(r'D[ée]tails|R[ée]server', text, re.I):
            parts.append(text)
    description = '\n'.join(parts)
    return description if len(description) >= 40 else None


def parse_detail(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    article = soup.select_one('article.lmdl-event-single')
    if not article:
        return None
    title = clean_text(article.select_one('.lmdl-event-single__title'))
    time_element = article.select_one('.lmdl-event-single__schedule-line time[datetime]')
    datetime_value = clean_text(time_element.get('datetime')) if time_element else ''
    try:
        event_date = date.fromisoformat(datetime_value[:10]).isoformat()
    except ValueError:
        event_date = None
    time_from, time_to = parse_times(
        article.select_one('.lmdl-event-single__schedule-line')
    )
    venue = clean_text(article.select_one('.lmdl-event-single__venue-name'))
    place = clean_text(article.select_one('.lmdl-event-single__place'))
    place_parts = [part.strip() for part in place.rsplit(',', 1)]
    city = place_parts[0] if place_parts else ''
    country_code = None
    if len(place_parts) == 2:
        country_code = COUNTRY_CODES.get(normalized(place_parts[1]))
    if len(place_parts) < 2 and city:
        country_code = country_for_city(city)

    description_parts = []
    for element in article.select(
        '.lmdl-event-single__description, .lmdl-event-single__content, '
        '.entry-content, .event-description'
    ):
        text = clean_text(element)
        if text and text not in description_parts:
            description_parts.append(text)
    description = '\n\n'.join(description_parts) or None
    if (
        not title or not event_date or not venue or not city or not country_code
        or normalized(venue) == normalized(city)
    ):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(url),
        'time_from': time_from,
        'time_to': time_to,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def fetch_event(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return parse_detail(response.text, url)


class MusiciensDuLouvreFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musiciensdulouvre_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'time_to', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        event_urls = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'id',
                    'order': 'asc',
                    '_fields': 'id,link',
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            events = response.json()
            event_urls.extend(
                canonical_url(event.get('link')) for event in events if event.get('link')
            )
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        records = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch_event, url): url for url in sorted(set(event_urls))
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape Les Musiciens du Louvre event detail',
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
                        'Skipped incomplete Les Musiciens du Louvre event',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, venue, city, or country is missing',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    MusiciensDuLouvreFrCrawler().run()


if __name__ == '__main__':
    main()
