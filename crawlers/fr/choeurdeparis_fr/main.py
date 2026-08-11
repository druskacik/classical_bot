import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.choeurdeparis.fr/'
SOURCE = 'Chœur de Paris'
EVENT_API_URL = (
    f'{SOURCE_URL}wp-content/plugins/6tem9Event/ajax/listEvent.php'
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}
MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3,
    'avril': 4, 'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'aout': 8, 'septembre': 9, 'octobre': 10, 'novembre': 11,
    'décembre': 12, 'decembre': 12,
}
PARIS_VENUES = (
    'blancs manteaux', 'oratoire du louvre', 'saint-roch', 'saint roch',
    'sainte-clotilde', 'sainte clotilde', 'pavillon de la sirène',
    'pavillon de la sirene', 'sorbonne', 'saint-louis-en l\'ile',
    'saint-louis-en-l\'ile', 'salle gaveau', 'temple de pentemont',
    'saint-germain-des-prés', 'saint-germain-des-pres',
    'saint germain des près', 'saint germain des pres',
    'saint germain l\'auxerrois', 'église de la madeleine',
    'eglise de la madeleine', 'église de la trinité',
    'eglise de la trinite', 'eglise des billettes',
    'notre dame du val de grâce', 'notre dame du val de grace',
    'notre dame d\'auteuil', 'nd d\'auteuil',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def parse_datetime(value):
    match = re.search(
        r'\bLe\s+(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})'
        r'(?:\s+(?:à|de)\s+(\d{1,2})h(\d{2}))?',
        value,
        re.I,
    )
    if not match:
        return None, None
    month = MONTHS.get(match.group(2).lower())
    if not month:
        return None, None
    try:
        event_date = date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None, None
    event_time = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5))
        if hour > 23 or minute > 59:
            return None, None
        event_time = f'{hour:02d}:{minute:02d}'
    return event_date, event_time


def clean_venue(value):
    venue = clean_text(value)
    venue = re.sub(r'^[-,\s]+|[-,\s]+$', '', venue)
    venue = re.sub(r'\s*,?\s*\d{1,3}\s+(?:rue|place|avenue|boulevard)\b.*$', '', venue, flags=re.I)
    venue = re.sub(r'\s*[-,]\s*Paris(?:\s+\d+(?:e|ème)?)?\s*$', '', venue, flags=re.I)
    venue = re.sub(r'\s+Paris(?:\s+\d+(?:e|ème)?)?\s*$', '', venue, flags=re.I)
    venue = re.sub(r'\s+\d{5}\s*,?\s*Paris\s*$', '', venue, flags=re.I)
    return venue.strip(' ,-')


def city_for_venue(raw_venue, venue):
    lowered = raw_venue.lower()
    if re.search(r'\bparis\b|\b75\d{3}\b', lowered):
        return 'Paris'
    if any(name in lowered for name in PARIS_VENUES):
        return 'Paris'
    if 'notre dame de bû' in lowered or 'notre dame de bu' in lowered:
        return 'Bû'
    if 'théatre lyrique saint marcel' in lowered or 'theatre lyrique saint marcel' in lowered:
        return 'Saint-Marcel'

    # These entries expose only one or more city names in the venue field.
    # Returning them as venues would violate the crawler record contract.
    city_only = {
        'saint nectaire', 'saint-nectaire', 'le mont-dore', 'mont dore',
        'ussy sur marne', 'epinal et saint dié', 'epinal et saint die',
        'mont dore et saint nectaire',
    }
    if venue.lower() in city_only:
        return None

    # The calendar belongs to a Paris choir and its otherwise unqualified
    # church/hall entries are established Paris venues.
    return 'Paris'


def parse_card(html):
    soup = BeautifulSoup(html, 'html.parser')
    datetime_heading = soup.select_one('.media-body > h3')
    title_link = soup.select_one('.media-body > h3 a')
    location = soup.select_one('.infosPost')
    if not datetime_heading or not title_link or not location:
        return None

    event_date, time_from = parse_datetime(clean_text(datetime_heading))
    title = clean_text(title_link)
    url = canonical_url(title_link.get('href', ''))
    raw_venue = clean_text(location)
    venue = clean_venue(raw_venue)
    city = city_for_venue(raw_venue, venue)
    if not title or not event_date or not url or not venue or not city:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
    }


def parse_description(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('#container-content .content')
    if not content:
        return None
    for element in content.select(
        '.m-bottom-10, #map_canvas, .clearfix, script, style, noscript'
    ):
        element.decompose()
    return clean_text(content) or None


def fetch_description(record):
    response = requests.get(record['url'], headers=HEADERS, timeout=45)
    response.raise_for_status()
    record['description'] = parse_description(response.text)
    return record


class ChoeurdeparisFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='choeurdeparis_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def fetch_view(self, view):
        first_response = requests.get(
            EVENT_API_URL,
            params={'view': view, 'category': 'allCategories', 'page': 1},
            headers=HEADERS,
            timeout=45,
        )
        first_response.raise_for_status()
        first_page = first_response.json()
        count_field = 'nbIncoming' if view == 'incoming' else 'nbPassed'
        total = int(first_page.get(count_field) or 0)
        page_count = math.ceil(total / 12)
        cards = list(first_page.get('result', []))

        for page in range(2, page_count + 1):
            response = requests.get(
                EVENT_API_URL,
                params={
                    'view': view, 'category': 'allCategories', 'page': page,
                },
                headers=HEADERS,
                timeout=45,
            )
            response.raise_for_status()
            cards.extend(response.json().get('result', []))
        return cards

    def scrape(self):
        cards = self.fetch_view('incoming') + self.fetch_view('passed')

        records = []
        for card in cards:
            record = parse_card(card)
            if record:
                records.append(record)
            else:
                log_message(
                    'Skipped incomplete Chœur de Paris event',
                    event='crawler_item_skipped',
                    level='warning',
                    error_type='IncompleteEventData',
                    error_message='Required date, title, URL, venue, or city is missing',
                )

        enriched = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_description, record): record for record in records}
            for future in as_completed(futures):
                record = futures[future]
                try:
                    enriched.append(future.result())
                except requests.RequestException as error:
                    record['description'] = None
                    enriched.append(record)
                    log_message(
                        'Failed to scrape Chœur de Paris event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            enriched,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    ChoeurdeparisFrCrawler().run()


if __name__ == '__main__':
    main()
