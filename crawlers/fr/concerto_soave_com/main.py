import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.concerto-soave.com/fr/'
SOURCE = 'Concerto Soave'
AGENDA_URLS = (
    urljoin(SOURCE_URL, 'agenda'),
    urljoin(SOURCE_URL, 'agenda-past'),
)
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}
COUNTRY_NAMES = {
    'allemagne': 'DE', 'autriche': 'AT', 'belgique': 'BE',
    'danemark': 'DK', 'espagne': 'ES', 'italie': 'IT',
    'monaco': 'MC', 'pays-bas': 'NL', 'portugal': 'PT',
    'royaume-uni': 'GB', 'suisse': 'CH',
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
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value.lower())
        if not unicodedata.combining(character)
    )


def parse_date(value):
    match = re.fullmatch(r'(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})', clean_text(value))
    if not match:
        return None
    month = MONTHS.get(normalized(match.group(2)))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_location(value):
    location = clean_text(value)
    location = re.sub(r'^\s*\uf041\s*', '', location)
    country_code = 'FR'
    country_match = re.search(r'\(([^()]*)\)\s*$', location)
    if country_match:
        candidate = normalized(country_match.group(1).strip())
        if candidate in COUNTRY_NAMES:
            country_code = COUNTRY_NAMES[candidate]
            location = location[:country_match.start()].strip()

    parts = [part.strip() for part in re.split(r'\s+-\s+', location) if part.strip()]
    if len(parts) < 2:
        return '', '', country_code
    if normalized(parts[-1]) in ('france', 'fr') and len(parts) >= 3:
        parts.pop()
    last_part = parts[-1]
    if ',' in last_part:
        venue_tail, city = [part.strip() for part in last_part.rsplit(',', 1)]
        venue = ' - '.join(parts[:-1] + [venue_tail]).strip()
    else:
        city = re.sub(r'\s*\|.*$', '', last_part)
        city = re.sub(r'^\d{5}\s+', '', city)
        city = re.sub(r'\s*\(\d{2,3}\)\s*$', '', city).strip()
        venue = ' - '.join(parts[:-1]).strip()
        venue_words = (
            'abbaye', 'auditorium', 'basilique', 'cathedrale', 'chapelle',
            'chateau', 'couvent', 'eglise', 'opera', 'salle', 'temple', 'theatre',
        )
        if normalized(city).startswith(venue_words):
            return '', '', country_code
    if not city or not venue or normalized(city) == normalized(venue):
        return '', '', country_code
    return city, venue, country_code


def parse_card(card, page_url):
    title = clean_text(card.select_one('.infos h2'))
    event_date = parse_date(card.select_one('.date'))
    city, venue, country_code = parse_location(card.select_one('.lieu'))
    external_link = card.select_one('.link-place a[href]')
    url = urljoin(page_url, external_link.get('href')) if external_link else page_url

    description_parts = []
    info = card.select_one('.infos')
    if info:
        direct_paragraph = info.find('p', recursive=False)
        repertoire = clean_text(direct_paragraph)
        if repertoire:
            description_parts.append(repertoire)
    details = clean_text(card.select_one('.infos-plus'))
    if details:
        description_parts.append(details)
    description = '\n\n'.join(dict.fromkeys(description_parts)) or None
    time_from = None
    time_match = re.search(r'\b([01]?\d|2[0-3])\s*h(?:\s*([0-5]\d))?\b', description or '', re.I)
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'

    if not title or not event_date or not city or not venue or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class ConcertoSoaveComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='concerto_soave_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        for page_url in AGENDA_URLS:
            response = requests.get(page_url, headers=HEADERS, timeout=45)
            response.raise_for_status()
            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.select('.bloc-agenda'):
                record = parse_card(card, page_url)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Concerto Soave concert',
                        event='crawler_item_skipped',
                        level='warning',
                        url=page_url,
                        error_type='IncompleteEventData',
                        error_message='Required date, city, or venue could not be extracted',
                    )
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    ConcertoSoaveComCrawler().run()


if __name__ == '__main__':
    main()
