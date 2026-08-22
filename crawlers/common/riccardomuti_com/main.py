import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.riccardomuti.com/'
API_URL = f'{SOURCE_URL}wp-json/tribe/events/v1/events'
SOURCE = 'Riccardo Muti'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)',
    'Accept': 'application/json',
    'Accept-Language': 'it-IT,it;q=0.9,en;q=0.7',
}

COUNTRY_ALIASES = {
    'argentina': 'AR', 'australia': 'AU', 'austria': 'AT', 'belgio': 'BE',
    'belgium': 'BE', 'brasile': 'BR', 'canada': 'CA', 'cina': 'CN',
    'china': 'CN', 'corea del sud': 'KR', 'croazia': 'HR', 'egitto': 'EG',
    'francia': 'FR', 'france': 'FR', 'germania': 'DE', 'germany': 'DE',
    'giappone': 'JP', 'japan': 'JP', 'giordania': 'JO', 'grecia': 'GR',
    'italia': 'IT', 'italy': 'IT', 'lettonia': 'LV', 'lussemburgo': 'LU',
    'monaco': 'MC', 'paesi bassi': 'NL', 'polonia': 'PL', 'regno unito': 'GB',
    'repubblica ceca': 'CZ', 'russia': 'RU', 'slovenia': 'SI', 'spagna': 'ES',
    'spain': 'ES', 'stati uniti': 'US', 'svizzera': 'CH', 'svezia': 'SE',
    'ungheria': 'HU', 'usa': 'US', 'united kingdom': 'GB',
    'united states': 'US', 'ucraina': 'UA', 'vaticano': 'VA',
}

CITY_COUNTRIES = {
    'aalborg': 'DK', 'agrigento': 'IT', 'aquileia': 'IT', 'ascoli piceno': 'IT',
    'athens': 'GR', 'atene': 'GR', 'baden-baden': 'DE', 'bari': 'IT',
    'berkeley': 'US', 'berlin': 'DE', 'berlino': 'DE', 'bologna': 'IT',
    'brussels': 'BE', 'bruxelles': 'BE', 'budapest': 'HU', 'busseto': 'IT',
    'cairo': 'EG', 'chicago': 'US', 'colonia': 'DE', 'cologne': 'DE',
    'como': 'IT', 'costa mesa': 'US', 'davis': 'US', 'essen': 'DE',
    'ferrara': 'IT', 'firenze': 'IT', 'florence': 'IT', 'francoforte': 'DE',
    'geneva': 'CH', 'genova': 'IT', 'ginevra': 'CH', 'granada': 'ES',
    'graz': 'AT', 'jerash': 'JO', 'jesi': 'IT', 'jurmala': 'LV', 'kiev': 'UA',
    'lampedusa': 'IT', 'london': 'GB', 'londra': 'GB', 'los angeles': 'US',
    'lucca': 'IT', 'lugano': 'CH', 'lussemburgo': 'LU', 'luxembourg': 'LU',
    'madrid': 'ES', 'melbourne': 'AU', 'mesa': 'US', 'miami': 'US',
    'milan': 'IT', 'milano': 'IT', 'monaco': 'DE', 'monte-carlo': 'MC',
    'naples': 'US', 'napoli': 'IT', 'new york': 'US', 'northridge': 'US',
    'norcia': 'IT', 'osaka': 'JP', 'ostuni': 'IT', 'palermo': 'IT',
    'palm desert': 'US', 'parigi': 'FR', 'paris': 'FR', 'pechino': 'CN',
    'piacenza': 'IT', 'pompei': 'IT', 'ravenna': 'IT', 'reggio emilia': 'IT',
    'rimini': 'IT', 'roma': 'IT', 'rome': 'IT', 'salzburg': 'AT',
    'salisburgo': 'AT', 'san diego': 'US', 'sarajevo': 'BA',
    'santa barbara': 'US', 'sarasota': 'US', 'seoul': 'KR', 'shanghai': 'CN',
    'spoleto': 'IT', 'stillwater': 'US', 'sydney': 'AU', 'taipei': 'TW',
    'tokyo': 'JP', 'torino': 'IT', 'turin': 'IT', 'venezia': 'IT',
    'vienna': 'AT', 'washington d.c.': 'US', 'washington': 'US',
    'west palm beach': 'US', 'wheaton': 'US', 'yokohama': 'JP', 'zurich': 'CH',
}


def clean_text(value):
    if value is None:
        return ''
    raw = str(value)
    if '<' not in raw and '>' not in raw:
        return html.unescape(raw).replace('\xa0', ' ').replace('\u200b', '').strip()
    soup = BeautifulSoup(raw, 'html.parser')
    for br in soup.find_all('br'):
        br.replace_with('\n')
    text = html.unescape(soup.get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()


def country_code(value):
    key = normalized(value or '')
    if len(key) == 2 and key.upper() in set(COUNTRY_ALIASES.values()):
        return key.upper()
    for name, code in COUNTRY_ALIASES.items():
        if normalized(name) == key:
            return code
    return None


def city_from_text(*values):
    combined = normalized(' '.join(value for value in values if value))
    matches = [city for city in CITY_COUNTRIES if re.search(rf'\b{re.escape(normalized(city))}\b', combined)]
    if not matches:
        return None
    return max(matches, key=len).title().replace('D.c.', 'D.C.')


def infer_location(event, description):
    venue_data = event.get('venue') or {}
    venue = clean_text(venue_data.get('venue'))
    city = clean_text(venue_data.get('city'))
    explicit_country = country_code(venue_data.get('country'))

    title = clean_text(event.get('title'))
    search_text = ' '.join((title, description, venue, city))
    if not city:
        leading_description = ' '.join(description.splitlines()[:5])
        city = city_from_text(title) or city_from_text(leading_description) or city_from_text(search_text) or ''

    code = explicit_country or CITY_COUNTRIES.get(city.casefold())
    if not code:
        for name, candidate in COUNTRY_ALIASES.items():
            if re.search(rf'\b{re.escape(normalized(name))}\b', normalized(search_text)):
                code = candidate
                break

    if not venue:
        lines = [line.strip(' -–—,') for line in description.splitlines() if line.strip()]
        city_pattern = re.compile(rf'\b{re.escape(city)}\b', re.I) if city else None
        ranked_lines = sorted(
            enumerate(lines[:12]),
            key=lambda item: (
                0 if city_pattern and city_pattern.search(item[1]) else 1,
                0 if re.search(r'\b(?:hall|teatro|theatre|festival|auditorium|arena|anfiteatro|musikverein|philharmonie|palazzo|piazza|basilica|cattedrale|cathedral|opera|center|centre|concertgebouw|carnegie)\b', item[1], re.I) else 1,
                item[0],
            ),
        )
        for _, line in ranked_lines:
            candidate = re.sub(r'\b(?:ore|at)\s+\d{1,2}(?::|\.)\d{2}.*$', '', line, flags=re.I).strip(' -–—,')
            candidate = re.sub(r',?\s*' + re.escape(city) + r'(?:,.*)?$', '', candidate, flags=re.I).strip(' -–—,') if city else candidate
            if (
                candidate and candidate.casefold() != city.casefold()
                and not re.fullmatch(r'\d{1,2}(?:[./-]\d{1,2})?(?:[./-]\d{2,4})?', candidate)
                and not re.search(r'\b(?:cancellat|annullat|sospes|postponed|cancelled)\b', candidate, re.I)
                and not re.match(r'^(?:programma|program|music|musica|riccardo muti|\d+(?:st|nd|rd|th) subscription concert)\b', candidate, re.I)
                and len(candidate) <= 160
            ):
                venue = candidate
                break

    if not venue or not city or not code or venue.casefold() == city.casefold():
        return None
    return venue, city, code


def parse_event(event):
    title = clean_text(event.get('title'))
    url = clean_text(event.get('url'))
    description = clean_text(event.get('description'))
    try:
        start = datetime.strptime(event['start_date'], '%Y-%m-%d %H:%M:%S')
    except (KeyError, TypeError, ValueError):
        return None

    location = infer_location(event, description)
    if not title or not url or not location:
        return None

    all_day = bool(event.get('all_day'))
    time_from = None if all_day else start.strftime('%H:%M')
    if all_day:
        time_match = re.search(r'\b(?:ore|at)\s+(\d{1,2})[:.](\d{2})\b', description, re.I)
        if time_match and 0 <= int(time_match.group(1)) <= 23:
            time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'

    venue, city, code = location
    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class RiccardomutiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='riccardomuti_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            params = {
                'per_page': 50,
                'page': page,
                'start_date': '2000-01-01 00:00:00',
                'end_date': '2100-12-31 23:59:59',
                'status': 'publish',
            }
            try:
                response = session.get(API_URL, params=params, timeout=60)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Riccardo Muti events API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            total_pages = int(payload.get('total_pages') or 1)
            for event in payload.get('events', []):
                record = parse_event(event)
                if record:
                    records.append(record)
            page += 1

        log_message(
            'Riccardo Muti events parsed',
            event='crawler_scrape_summary',
            record_count=len(records),
            page_count=total_pages,
        )
        return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


def main():
    RiccardomutiComCrawler().run()


if __name__ == '__main__':
    main()
