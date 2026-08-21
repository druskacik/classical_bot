import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'Gianandrea Noseda'
SOURCE_URL = 'https://www.gianandreanoseda.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule.aspx')
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0; +https://classical.bot)',
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

# The touring calendar supplies a city but not a country. These mappings cover
# the international cities used in the published schedule. Unknown locations
# are skipped instead of being assigned Noseda's home country.
CITY_COUNTRIES = {
    'abu dhabi': 'AE', 'amsterdam': 'NL', 'athens': 'GR', 'atlanta': 'US',
    'barcelona': 'ES', 'beijing': 'CN', 'berlin': 'DE', 'birmingham': 'GB',
    'boston': 'US', 'brussels': 'BE', 'budapest': 'HU', 'chicago': 'US',
    'cincinnati': 'US', 'copenhagen': 'DK', 'dallas': 'US', 'dresden': 'DE',
    'edinburgh': 'GB', 'florence': 'IT', 'frankfurt': 'DE', 'geneva': 'CH',
    'hamburg': 'DE', 'hong kong': 'HK', 'london': 'GB', 'los angeles': 'US',
    'lucerne': 'CH', 'madrid': 'ES', 'manchester': 'GB', 'melbourne': 'AU',
    'milan': 'IT', 'milano': 'IT', 'montreal': 'CA', 'montreaux': 'CH',
    'montreux': 'CH', 'moscow': 'RU', 'munich': 'DE', 'naples': 'IT',
    'new york': 'US', 'new york city': 'US', 'osaka': 'JP', 'oslo': 'NO',
    'ottawa': 'CA', 'paris': 'FR', 'philadelphia': 'US', 'prague': 'CZ',
    'rome': 'IT', 'salzburg': 'AT', 'san francisco': 'US', 'seoul': 'KR',
    'shanghai': 'CN', 'singapore': 'SG', 'st. petersburg': 'RU',
    'stockholm': 'SE', 'sydney': 'AU', 'tbilisi': 'GE', 'tel aviv': 'IL',
    'tokyo': 'JP', 'torino': 'IT', 'toronto': 'CA', 'turin': 'IT',
    'venice': 'IT', 'vienna': 'AT', 'washington': 'US',
    'washington, dc': 'US', 'washington d.c.': 'US', 'warsaw': 'PL',
    'zurich': 'CH', 'zürich': 'CH',
}

# A few schedule rows put a well-known festival or venue in the field normally
# used for the city. Each replacement is supported by the linked organizer.
LOCATION_ALIASES = {
    'edinburgh festival': ('Edinburgh', 'GB'),
    'tsinandali festival': ('Tsinandali', 'GE'),
    'tsinandali festival opening concert': ('Tsinandali', 'GE'),
    'wolf trap': ('Vienna', 'US'),
}

CITY_NORMALIZATIONS = {
    'baden baden': 'Baden-Baden', 'geneve': 'Geneva', 'koln': 'Cologne',
    'lisboa': 'Lisbon', 'luzern': 'Lucerne', 'munchen': 'Munich',
    'münchen': 'Munich', 'napoli': 'Naples', 'praha': 'Prague',
    'roma': 'Rome', 's.petersburg': 'St. Petersburg', 'sevilla': 'Seville',
    'gstaad menuhin festival': 'Gstaad',
    'locarno settimane musicali di ascona': 'Locarno', 'london proms': 'London',
    'milano mito festival': 'Milano', 'stockhom nobel prize': 'Stockholm',
    'torino mito festival': 'Torino', 'torino mito festival opening': 'Torino',
    'wien': 'Vienna',
    'washington d.c.': 'Washington',
}

TLD_COUNTRIES = {
    'at': 'AT', 'au': 'AU', 'be': 'BE', 'ca': 'CA', 'ch': 'CH', 'cn': 'CN',
    'cz': 'CZ', 'de': 'DE', 'dk': 'DK', 'es': 'ES', 'fi': 'FI', 'fr': 'FR',
    'ge': 'GE', 'gr': 'GR', 'hk': 'HK', 'ie': 'IE', 'il': 'IL', 'it': 'IT',
    'jp': 'JP', 'kr': 'KR', 'nl': 'NL', 'no': 'NO', 'pl': 'PL', 'pt': 'PT',
    'ru': 'RU', 'se': 'SE', 'sg': 'SG', 'uk': 'GB',
}


def clean_text(node):
    if node is None:
        return ''
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip(' ,')


def country_for(city, url):
    code = CITY_COUNTRIES.get(city.casefold())
    if code:
        return code
    hostname = (urlparse(url).hostname or '').lower()
    suffix = hostname.rsplit('.', 1)[-1]
    return TLD_COUNTRIES.get(suffix)


def normalize_location(raw_city):
    folded = raw_city.casefold()
    if folded in LOCATION_ALIASES:
        return LOCATION_ALIASES[folded]
    if folded.startswith('edinburgh international festival'):
        return 'Edinburgh', 'GB'
    if folded.startswith('tsinandali festival'):
        return 'Tsinandali', 'GE'
    if folded in CITY_NORMALIZATIONS:
        return CITY_NORMALIZATIONS[folded], None
    for suffix in (' mito festival opening', ' mito festival', ' festival opening', ' festival 2022', ' festival'):
        if folded.endswith(suffix):
            raw_city = raw_city[:-len(suffix)].strip()
            folded = raw_city.casefold()
            break
    city = CITY_NORMALIZATIONS.get(folded, raw_city)
    return city, None


def request_schedule(session, year, month, response):
    soup = BeautifulSoup(response.text, 'html.parser')
    form = {
        field.get('name'): field.get('value', '')
        for field in soup.select('form#ctl00 input[name]')
    }
    form.update({
        'drpYear': str(year),
        'drpMonth': f'{month:02d}',
        'hiddenData': f'{year}{month:02d}01',
        'UploadButton': '',
    })
    for attempt in range(4):
        try:
            result = session.post(SCHEDULE_URL, data=form, timeout=45)
            if result.status_code == 429 and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            result.raise_for_status()
            return result
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError('Schedule request retry loop ended unexpectedly')


def parse_page(html, requested_year, request_month):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for item in soup.select('#mainContainer .schedule_container'):
        month_name = clean_text(item.select_one('.s_mese')).lower()[:3]
        days_text = clean_text(item.select_one('.s_giorno'))
        raw_city = clean_text(item.select_one('.s_corpo h2'))
        city, alias_country = normalize_location(raw_city)
        ensemble = clean_text(item.select_one('.s_corpo h3'))
        link = item.select_one('a.s_link[href]')
        link_text = clean_text(link)
        venue, separator, programme = link_text.partition('/')
        venue = venue.strip()
        programme = programme.strip()
        title = f'{ensemble} — {programme}' if separator and programme else ensemble
        url = urljoin(SCHEDULE_URL, link.get('href', '').strip()) if link else ''
        month = MONTHS.get(month_name)
        country_code = alias_country or (country_for(city, url) if city and url else None)
        invalid_location = (
            ',' in city
            or re.search(r'(?i)\bon[ -]?line event\b', raw_city)
            or re.fullmatch(r'(?i)(?:open air concert|online event|streaming)', venue)
        )
        if invalid_location or not all((month, city, title, venue, url, country_code)):
            log_message(
                'Skipping incomplete Gianandrea Noseda schedule entry',
                event='crawler_item_skipped',
                level='warning',
                url=url or SCHEDULE_URL,
                error_type='IncompleteEventData',
                error_message='Date, city, venue, URL, or country could not be resolved',
            )
            continue

        # A month request returns the next ten entries and can cross New Year.
        # Any lower-numbered month returned after the requested month belongs
        # to the next calendar year.
        year = requested_year + (1 if month < request_month else 0)
        for raw_day in re.findall(r'\d{1,2}', days_text):
            try:
                event_date = date(year, month, int(raw_day)).isoformat()
            except ValueError:
                continue
            description = '\n'.join(part for part in (ensemble, programme, venue, city) if part)
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class GianandreaNosedaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='gianandreanoseda_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        log_message('Fetching Gianandrea Noseda schedule', event='crawler_url_fetch', url=SCHEDULE_URL)
        response = session.get(SCHEDULE_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        years = sorted({
            int(option.get('value'))
            for option in soup.select('#drpYear option[value]')
            if option.get('value', '').isdigit()
        })
        if not years:
            raise ValueError('Schedule did not expose year options')

        records = []
        seen = set()
        for year in years:
            for month in range(1, 13):
                response = request_schedule(session, year, month, response)
                for record in parse_page(response.text, year, month):
                    key = (record['title'], record['date'], record['venue'], record['city'])
                    if key not in seen:
                        seen.add(key)
                        records.append(record)
                time.sleep(1)

        log_message(
            'Gianandrea Noseda schedule parsed',
            event='crawler_records_parsed',
            url=SCHEDULE_URL,
            record_count=len(records),
        )
        return sorted(records, key=lambda row: (row['date'], row['city'], row['venue'], row['title']))


def main():
    GianandreaNosedaComCrawler().run()


if __name__ == '__main__':
    main()
