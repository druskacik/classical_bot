import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://joycedidonato.com/'
CALENDAR_URL = f'{SOURCE_URL}performances/'
LOAD_MORE_URL = f'{SOURCE_URL}wp-content/themes/joycedidonato/load-more.php'
SOURCE = 'Joyce DiDonato'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.8',
}

MONTH_ALIASES = {
    name: number for number, names in enumerate((
        ('JAN', 'JANUARY'), ('FEB', 'FEBRUARY'), ('MAR', 'MARCH'),
        ('APR', 'APRIL'), ('MAY',), ('JUN', 'JUNE'), ('JUL', 'JULY'),
        ('AUG', 'AUGUST'), ('SEP', 'SEPT', 'SEPTEMBER'), ('OCT', 'OCTOBER'),
        ('NOV', 'NOVEMBER'), ('DEC', 'DECEMBER'),
    ), 1) for name in names
}

# The site is an international touring calendar and supplies a city, but not a
# country field. These overrides cover ambiguous generic top-level domains and
# names whose displayed form is not itself a city. Country-code TLDs are handled
# separately below.
CITY_COUNTRIES = {
    'amsterdam': 'NL', 'aspen, co': 'US', 'athens': 'GR', 'atlanta, ga': 'US',
    'austin, tx': 'US', 'baden-baden': 'DE', 'barcelona': 'ES', 'berlin': 'DE',
    'birmingham': 'GB', 'boston, ma': 'US', 'brussels': 'BE', 'budapest': 'HU',
    'chicago, il': 'US', 'cincinnati, oh': 'US', 'cleveland, oh': 'US',
    'dallas, tx': 'US', 'dresden': 'DE', 'dublin': 'IE', 'edinburgh': 'GB',
    'essen': 'DE', 'florence': 'IT', 'frankfurt': 'DE', 'geneva': 'CH',
    'gstaad': 'CH', 'hamburg': 'DE', 'houston, tx': 'US', 'kansas city, mo': 'US',
    'la jolla, ca': 'US', 'leipzig': 'DE', 'lille': 'FR', 'lisbon': 'PT',
    'london': 'GB', 'los angeles, ca': 'US', 'lucerne': 'CH', 'luxembourg': 'LU',
    'madrid': 'ES', 'milan': 'IT', 'minneapolis, mn': 'US', 'monaco': 'MC',
    'montreal': 'CA', 'munich': 'DE', 'naples': 'IT', 'new york, ny': 'US',
    'oslo': 'NO', 'oxford': 'GB', 'paris': 'FR', 'philadelphia, pa': 'US',
    'prague': 'CZ', 'ravinia, il': 'US', 'rome': 'IT', 'salzburg': 'AT',
    'san francisco, ca': 'US', 'santa fe, nm': 'US', 'seattle, wa': 'US',
    'stockholm': 'SE', 'sydney': 'AU', 'tanglewood, ma': 'US', 'tampere': 'FI',
    'toronto': 'CA', 'toulouse': 'FR', 'valencia': 'ES', 'venice': 'IT',
    'versailles': 'FR', 'vienna': 'AT', 'vilnius': 'LT', 'warsaw': 'PL',
    'washington, dc': 'US', 'zurich': 'CH', 'bochum': 'DE', 'grafenegg': 'AT',
    'new york': 'US', 'new york city': 'US', 'kansas city': 'US', 'chicago': 'US',
    'dallas': 'US', 'houston': 'US', 'santa fe': 'US', 'san francisco': 'US',
    'princeton': 'US', 'princeton, nj': 'US', 'mansfield, ct': 'US',
    'ann arbor': 'US', 'akron': 'US', 'santa monica, ca': 'US',
    'washington d.c.': 'US', 'atlanta, ga': 'US', 'fort worth, tx': 'US',
    'buenos aires': 'AR', 'hong kong': 'HK', 'shanghai': 'CN', 'beijing': 'CN',
    'tokyo': 'JP', 'strasbourg': 'FR', 'montréal': 'CA', 'montreal': 'CA',
    'oviedo': 'ES', 'são paulo': 'BR', 'rio de janeiro': 'BR', 'moscow': 'RU',
    'santiago': 'CL', 'lyon': 'FR', 'santo domingo': 'DO', 'saffron walden': 'GB',
    'québec': 'CA', 'st petersburg': 'RU', 'bucharest': 'RO', 'istanbul': 'TR',
    'mexico city': 'MX', 'milano': 'IT', 'bregenz': 'AT', 'peralada': 'ES',
    'sofia': 'BG', 'kaohsiung': 'TW', 'ordino': 'AD',
    'la côte-saint-andré': 'FR', 'bilbao': 'ES', 'antwerp': 'BE',
    'torroella': 'ES', 'santander': 'ES', 'bogotá': 'CO',
    'vilabertran': 'ES', 'auvers-sur-oise': 'FR', 'saint-denis': 'FR',
    'boston': 'US', 'abu dhabi': 'AE', 'lotte': 'DE', 'taipei': 'TW',
    'macau': 'MO', 'evian': 'FR', 'copenhagen': 'DK', 'rochester': 'US',
    'berkeley': 'US', 'stanford': 'US', 'vancouver': 'CA', 'seattle': 'US',
    'quito': 'EC', 'lima': 'PE', 'verbier': 'CH', 'singapore': 'SG',
    'muscat': 'OM', 'brooklyn, new york': 'US', 'sao paolo': 'BR',
    'montevideo': 'UY', 'beaver creek': 'US', 'palm beach': 'US',
    'monte carlo': 'MC', 'aspen': 'US', 'valladolid': 'ES', 'sonoma': 'US',
    'philadelphia': 'US', 'atlanta': 'US',
}

TLD_COUNTRIES = {
    'at': 'AT', 'au': 'AU', 'be': 'BE', 'ca': 'CA', 'ch': 'CH', 'cz': 'CZ',
    'de': 'DE', 'dk': 'DK', 'es': 'ES', 'fi': 'FI', 'fr': 'FR', 'gr': 'GR',
    'hr': 'HR', 'hu': 'HU', 'ie': 'IE', 'is': 'IS', 'it': 'IT', 'jp': 'JP',
    'kr': 'KR', 'lt': 'LT', 'lu': 'LU', 'lv': 'LV', 'mx': 'MX', 'nl': 'NL',
    'no': 'NO', 'nz': 'NZ', 'pl': 'PL', 'pt': 'PT', 'se': 'SE', 'si': 'SI',
    'sk': 'SK', 'uk': 'GB',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def flattened_params(value, prefix=''):
    result = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f'{prefix}[{key}]' if prefix else key
            result.extend(flattened_params(child, child_prefix))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(flattened_params(child, f'{prefix}[{index}]'))
    elif value is not None:
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        result.append((prefix, str(value)))
    return result


def get_response(session, url, **kwargs):
    response = session.get(url, timeout=45, **kwargs)
    response.raise_for_status()
    return response


def valid_date(year, month, day):
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def event_dates(text, default_year=None):
    """Expand the calendar's `AUG 21, 22, 23, 2026` style date labels."""
    normalized = re.sub(r'\s+', ' ', text.upper()).strip()
    year_match = re.search(r'\b(20\d{2})\b', normalized)
    year = year_match.group(1) if year_match else default_year
    if not year:
        return []
    date_text = normalized[:year_match.start()] if year_match else normalized
    current_month = None
    values = []
    for token in re.findall(r'[A-Z]+|\d{1,2}', date_text):
        if token in MONTH_ALIASES:
            current_month = MONTH_ALIASES[token]
        elif current_month:
            value = valid_date(year, current_month, token)
            if value:
                values.append(value)
    return values


def country_code(city, url):
    key = re.sub(r'\s+', ' ', city).strip().casefold()
    if key in CITY_COUNTRIES:
        return CITY_COUNTRIES[key]
    if re.search(r',\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[EDAINSOT]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])$', city, re.I):
        return 'US'
    if re.search(r',\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$', city, re.I):
        return 'CA'
    hostname = (urlparse(url).hostname or '').lower()
    suffix = hostname.rsplit('.', 1)[-1]
    return TLD_COUNTRIES.get(suffix)


def parse_article(article, default_year=None):
    title = clean_text(article.select_one('.press-item-title'))
    city = clean_text(article.select_one('.press-item-citation'))
    date_label = clean_text(article.select_one('.press-item-date'))
    excerpt = article.select_one('.performance-item-excerpt')
    description = clean_text(excerpt)
    venue_link = excerpt.select_one('a[href]') if excerpt else None
    venue = clean_text(venue_link)
    if not venue and excerpt:
        first_line = clean_text(excerpt).split('\n', 1)[0].strip()
        venue = first_line
    url = ''
    if venue_link:
        url = venue_link.get('href', '').strip()
    if not url:
        target = article.select_one('[data-href]')
        url = target.get('data-href', '').strip() if target else ''
    if not url and article.get('id'):
        url = f'{CALENDAR_URL}#{article["id"]}'
    code = country_code(city, url)
    dates = event_dates(date_label, default_year)
    if not all((title, city, venue, url, code, dates)):
        return []
    return [{
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': None,
        'venue': venue,
        'city': city,
        'country_code': code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    } for event_date in dates]


def page_articles(session, past):
    params = {'type': 'past'} if past else None
    soup = BeautifulSoup(get_response(session, CALENDAR_URL, params=params).text, 'html.parser')
    articles = list(soup.select('article.event-item'))
    button = soup.select_one('.js-load-more[data-data]')
    if not button:
        return articles
    load_data = json.loads(button['data-data'])
    total = int(load_data.get('total_posts', len(articles)))
    page_size = int(load_data.get('amount_to_load', 5))

    def fetch(offset):
        data = json.loads(json.dumps(load_data))
        data['post_count'] = offset
        html = get_response(session, LOAD_MORE_URL, params=flattened_params(data)).text
        return BeautifulSoup(html, 'html.parser').select('article.event-item')

    offsets = range(len(articles), total, page_size)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fetch, offset): offset for offset in offsets}
        pages = {}
        errors = []
        for future in as_completed(futures):
            offset = futures[future]
            try:
                pages[offset] = future.result()
            except requests.RequestException as error:
                errors.append(error)
                log_message(
                    'Failed to load Joyce DiDonato calendar page',
                    event='crawler_page_failed', level='warning',
                    url=LOAD_MORE_URL, offset=offset,
                    error_type=type(error).__name__, error_message=str(error),
                )
    if errors:
        raise errors[0]
    for offset in sorted(pages):
        articles.extend(pages[offset])
    return articles


class JoyceDiDonatoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='joycedidonato_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='classical',
        dedupe_subset=['url', 'date'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for past in (False, True):
            articles = page_articles(session, past)
            inferred_year = date.today().year
            previous_month = None
            for article in articles:
                label = clean_text(article.select_one('.press-item-date'))
                explicit_year = re.search(r'\b(20\d{2})\b', label)
                month_match = re.search(r'[A-Za-z]+', label)
                first_month = MONTH_ALIASES.get(month_match.group(0).upper()) if month_match else None
                if explicit_year:
                    inferred_year = int(explicit_year.group(1))
                elif past and previous_month and first_month and first_month > previous_month:
                    inferred_year -= 1
                records.extend(parse_article(article, inferred_year))
                if first_month:
                    previous_month = first_month
        unique = {(record['url'], record['date']): record for record in records}
        return sorted(unique.values(), key=lambda item: (item['date'], item['title'], item['city']))


def main():
    return JoyceDiDonatoCrawler().run()


if __name__ == '__main__':
    main()
