import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musikfabrik.eu/en/'
CALENDAR_URL = f'{SOURCE_URL}calendar/'
SOURCE = 'Ensemble Musikfabrik'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}

# The calendar is an ensemble tour calendar, not a venue calendar.  It only
# publishes a city (not a country) for most performances, so countries must be
# resolved from that first-party city value. Unknown and non-place labels are
# deliberately skipped instead of being assigned the ensemble's home country.
COUNTRIES_BY_CITY = {
    'DE': (
        'aachen', 'augsburg', 'bad doberan', 'bamberg', 'berlin', 'bochum',
        'bonn', 'cologne', 'darmstadt', 'detmold', 'donaueschingen', 'dortmund',
        'dresden', 'duisburg', 'düsseldorf', 'erftstadt', 'essen', 'fellbach',
        'frankfurt', 'gladbeck', 'gütersloh', 'hagen', 'hamburg', 'hannover',
        'herrenhausen', 'karlsruhe', 'kassel', 'kiel', 'kleve', 'krefeld',
        'kürten', 'köln', 'leipzig', 'leverkusen', 'lübeck', 'moers', 'munich',
        'münchen', 'müncheberg', 'mönchengladbach', 'münster', 'neuss',
        'oldenburg', 'osnabrück', 'pulheim-stommeln', 'rostock', 'salzgitter',
        'schwetzingen', 'solingen', 'stommeln', 'stuttgart', 'trier', 'unna',
        'weimar', 'weingarten', 'witten', 'wuppertal', 'würzburg',
    ),
    'AT': ('bludenz', 'bregenz', 'innsbruck', 'krems', 'salzburg', 'schwaz', 'vienna', 'wien'),
    'BE': ('brussels', 'eupen', 'leuven'),
    'CH': ('geneva', 'la chaux-de-fonds', 'zürich'),
    'CZ': ('brno', 'prag'),
    'DK': ('aarhus', 'copenhagen'),
    'EE': ('tallinn', 'tartu'),
    'ES': ('alicante', 'barcelona', 'bilbao', 'cuenca', 'madrid', 'sueca'),
    'FI': ('helsinki', 'viitasaari'),
    'FR': ('annecy', 'asnières-sur-oise', 'créteil', 'grenoble', 'lille', 'lyon',
           'metz', 'paris', 'reimes', 'royaumont', 'strasbour', 'strasbourg'),
    'GB': ('bristol', 'edinburgh', 'huddersfield', 'london'),
    'GR': ('athens', 'thessaloniki'),
    'HR': ('zagreb',),
    'HU': ('budapest', 'pécs'),
    'IE': ('dublin',),
    'IL': ('jaffa', 'jerusalem', 'tel aviv'),
    'IN': ('bangalore',),
    'IT': ('cremona', 'florence', 'mailand', 'parma', 'rom', 'rome', 'trient',
           'turin', 'venedig', 'venice'),
    'LT': ('vilnius',),
    'LU': ('luxembourg', 'luxemburg'),
    'LV': ('riga',),
    'NL': ('amsterdam', 'groningen', "'s-hertogenbosch", "s'-hertogenbosch",
           'maastricht', 'nijmegen', 'tilburg', 'utrecht'),
    'NO': ('bergen', 'oslo', 'trondheim'),
    'NZ': ('auckland', 'christchurch', 'dunedin', 'wellington'),
    'PL': ('breslau', 'cracow', 'krakau', 'krakow', 'stettin', 'warschau',
           'warsaw', 'wrocław'),
    'PT': ('lissabon', 'porto'),
    'RS': ('belgrad',),
    'SG': ('singapore',),
    'TH': ('bangkok',),
    'TW': ('taichung',),
    'UA': ('kiew',),
    'US': ('boston', 'chicago', 'harvard', 'new york', 'philadelphia', 'troy'),
    'VN': ('hanoi',),
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_years(session):
    soup = get_soup(session, CALENDAR_URL)
    years = []
    for option in soup.select('select[name="event-year"] option[value]'):
        value = option.get('value', '')
        if re.fullmatch(r'\d{4}', value):
            years.append(int(value))
    return sorted(set(years))


def listing_urls(session, year):
    soup = get_soup(session, CALENDAR_URL, params={'event-type': '', 'event-year': year})
    urls = set()
    for title in soup.select('.teaser-title'):
        link = title.find_parent('a', href=True)
        if link:
            urls.add(urljoin(CALENDAR_URL, link['href'].split('?')[0]))
    return urls


def country_for_city(city):
    normalized = clean_text(city).casefold().strip(' ,')
    normalized = re.sub(r'\s+nrw$', '', normalized).strip()
    if normalized == 'colognd':
        normalized = 'cologne'
    for country_code, names in COUNTRIES_BY_CITY.items():
        if normalized in names:
            return country_code
    return None


def parse_location(detail):
    table = detail.select_one('.calendar-detail-table')
    if not table:
        return None, None, None

    location = None
    for paragraph in table.find_all_next('p'):
        if paragraph.find_parent(class_='calendar-detail') != detail:
            break
        if paragraph.select_one('.btn-cta'):
            continue
        text = clean_text(paragraph)
        if text:
            location = paragraph
            break
    if not location:
        return None, None, None

    map_link = location.find('a', href=re.compile(r'google\.(?:com|de)/maps'))
    venue = clean_text(map_link) if map_link else ''
    raw = clean_text(location)
    if venue and raw.endswith(venue):
        city = raw[:-len(venue)].strip(' ,')
    elif ',' in raw:
        city, venue = (part.strip() for part in raw.split(',', 1))
    else:
        return None, None, None

    city = re.sub(r'\s+NRW$', '', city, flags=re.IGNORECASE).strip().strip(' ,')
    country_code = country_for_city(city)
    if not city or not venue or not country_code:
        return None, None, None
    return venue, city, country_code


def parse_date(value):
    match = re.search(r'(\d{1,2}\.\d{1,2}\.\d{4})', value)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    value = clean_text(value).lower().replace('.', ':')
    for pattern in ('%I:%M %p', '%I %p', '%H:%M', '%H'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            continue
    return None


def detail_description(soup, detail):
    parts = []
    heading = detail.select_one('h1')
    if heading:
        summary = heading.find_next_sibling('p')
        if summary and summary.find_next_sibling('table'):
            parts.append(clean_text(summary))
    center = soup.select_one('#center')
    if center:
        for block in center.select(':scope > .ce'):
            text = clean_text(block)
            if text:
                parts.append(text)
    description = clean_text('\n\n'.join(dict.fromkeys(parts)))
    return description or None


def parse_detail(session, url):
    soup = get_soup(session, url)
    detail = soup.select_one('.calendar-detail')
    if not detail:
        return []
    title = clean_text(detail.select_one('h1'))
    venue, city, country_code = parse_location(detail)
    if not title or not venue or not city or not country_code:
        return []

    description = detail_description(soup, detail)
    records = []
    for row in detail.select('.calendar-detail-table tr'):
        cells = row.find_all('td')
        if len(cells) < 2:
            continue
        event_date = parse_date(clean_text(cells[0]))
        if not event_date:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(cells[1]),
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    years = calendar_years(session)
    urls = set()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(listing_urls, session, year): year for year in years}
        for future in as_completed(futures):
            year = futures[future]
            try:
                urls.update(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape calendar year',
                    event='crawler_page_failed',
                    level='warning',
                    url=f'{CALENDAR_URL}?event-year={year}',
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(parse_detail, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(future.result())
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape concert detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class MusikfabrikEuCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musikfabrik_eu',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MusikfabrikEuCrawler().run()


if __name__ == '__main__':
    main()
