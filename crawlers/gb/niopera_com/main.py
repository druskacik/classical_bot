import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://niopera.com/'
SITEMAP_URL = f'{SOURCE_URL}performances-sitemap.xml'
SOURCE = 'Northern Ireland Opera'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = (
    'January|February|March|April|May|June|July|August|September|October|'
    'November|December'
)
DATE_GROUP_RE = re.compile(
    rf'(?P<days>\d{{1,2}}(?:st|nd|rd|th)?(?:\s*(?:,|&|and|to|[-–—])\s*'
    rf'\d{{1,2}}(?:st|nd|rd|th)?)*)\s+(?P<month>{MONTHS})'
    rf'(?:\s+(?P<year>20\d{{2}}))?',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'\b(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)\b', re.IGNORECASE)

# Places found in the company's performance archive. Longer names must be
# checked first (for example, Newcastle before New Castle-like fragments).
CITIES = (
    'Londonderry', 'Newtownards', 'Carrickfergus', 'Enniskillen', 'Portstewart',
    'Downpatrick', 'Ballymena', 'Coleraine', 'Hillsborough', 'Belfast', 'Armagh',
    'Bangor', 'Lisburn', 'Newcastle', 'Omagh', 'Newry', 'Derry', 'Perth',
)
CITY_RE = re.compile(r'\b(' + '|'.join(map(re.escape, CITIES)) + r')\b', re.IGNORECASE)

# Explicit venue names make the result more reliable than trying to turn an
# address or arbitrary prose before "in Belfast" into a venue.
VENUES = (
    'Grand Opera House', 'The Grand Opera House', 'Brian Friel Theatre',
    'The Brian Friel Theatre', 'Carlisle Memorial Church', 'Carlisle Memorial',
    'First Church', 'Custom House', 'Ards Arts Centre', 'Grand Central Hotel',
    'The Grand Central Hotel', 'The Seahorse', 'Lyric Theatre', 'The Lyric Theatre',
    'Ulster Hall', 'Waterfront Hall', 'The MAC', 'Theatre at the Mill',
    'Portico of Ards', 'Guildhall', 'St Anne’s Cathedral', "St Anne's Cathedral",
    'St George’s Church', "St George's Church", 'Clonard Monastery',
    'Queen’s Film Theatre', "Queen's Film Theatre", 'QFT', 'Second Nature',
    'The Studio Theatre', 'Studio Theatre', 'Black Box', 'Crescent Arts Centre',
    'Strand Arts Centre', 'Market Place Theatre', 'Roe Valley Arts Centre',
    'Enniskillen Castle', 'Duncairn Arts Centre', 'Belfast Cathedral',
)
VENUE_RE = re.compile(
    r'\b(' + '|'.join(sorted(map(re.escape, VENUES), key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_response(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response


def performance_urls(session):
    soup = BeautifulSoup(get_response(session, SITEMAP_URL).content, 'xml')
    urls = []
    for url_node in soup.find_all('url'):
        loc = url_node.find('loc', recursive=False)
        if not loc:
            continue
        url = clean_text(loc)
        path = urlparse(url).path
        if re.fullmatch(r'/performances/[^/]+/', path):
            urls.append(url)
    return list(dict.fromkeys(urls))


def page_year(text, title, url):
    for value in (title, url):
        match = re.search(r'\b(20\d{2})\b', value)
        if match:
            return int(match.group(1))
    # Production copy commonly compares the new show with older seasons before
    # stating its own year. The newest year in the opening copy is normally the
    # occurrence year and avoids assigning Eugene Onegin 2024 to its 2022
    # predecessor mentioned in the same paragraph.
    years = [int(value) for value in re.findall(r'\b(20\d{2})\b', text[:4000])]
    if years:
        return max(years)
    return None


def parse_dates(text, default_year):
    dates = []
    for match in DATE_GROUP_RE.finditer(text):
        year = int(match.group('year')) if match.group('year') else default_year
        if not year:
            continue
        month = match.group('month')
        raw_days = [int(day) for day in re.findall(r'\d{1,2}', match.group('days'))]
        # A written range generally denotes every performance day only when
        # the page explicitly lists them. Do not invent intermediate dates.
        for day in raw_days:
            try:
                value = datetime.strptime(f'{day} {month} {year}', '%d %B %Y').date().isoformat()
            except ValueError:
                continue
            if value not in dates:
                dates.append(value)
    return dates


def parse_time(text):
    match = TIME_RE.search(text)
    if not match:
        return None
    hour, minute, period = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower()
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if period == 'pm' and hour != 12:
        hour += 12
    elif period == 'am' and hour == 12:
        hour = 0
    return f'{hour:02d}:{minute:02d}'


def location_from_text(text):
    venue_match = VENUE_RE.search(text)
    if not venue_match:
        return None, None
    venue = re.sub(r'^the\s+', '', venue_match.group(1), flags=re.IGNORECASE)

    # Prefer the city printed close to the venue, then any city on the same
    # content block. QFT and the NI Opera home venues are explicitly Belfast.
    nearby = text[max(0, venue_match.start() - 100):venue_match.end() + 140]
    city_match = CITY_RE.search(nearby) or CITY_RE.search(text)
    city = city_match.group(1) if city_match else None
    if city and city.lower() == 'derry':
        city = 'Derry'
    belfast_venues = {
        'grand opera house', 'brian friel theatre', 'carlisle memorial church',
        'carlisle memorial', 'first church', 'custom house', 'grand central hotel',
        'the seahorse', 'lyric theatre', 'qft', 'queen’s film theatre',
        "queen's film theatre", 'second nature', 'ulster hall', 'waterfront hall',
        'the mac', 'clonard monastery', 'belfast cathedral',
    }
    # These venue names are unambiguously Belfast locations throughout the
    # archive; overriding prevents tour-related prose elsewhere in the article
    # from being mistaken for the occurrence city.
    if venue.lower() in belfast_venues:
        city = 'Belfast'
    return venue, city


def description_node(soup):
    article = soup.select_one('main article')
    if not article:
        return None
    for node in article.select('script, style, form, nav'):
        node.decompose()
    return clean_text(article) or None


def parse_event(content, url):
    soup = BeautifulSoup(content, 'html.parser')
    hero = soup.select_one('main .content-hero--show-detail')
    if not hero:
        return []
    title = clean_text(hero.select_one('h2.title'))
    date_label = clean_text(hero.select_one('h1.date'))
    description = description_node(soup)
    if not title or not date_label or not description:
        return []
    if urlparse(url).path.rstrip('/').endswith('/calendar-of-events'):
        return []
    if re.search(
        r'\b(?:skills expo|easter scheme|skills talk|opera adventurers|outreach week)\b',
        title,
        re.IGNORECASE,
    ):
        return []

    year = page_year(description, title, url)
    hero_dates = parse_dates(date_label, year)
    records = []

    if hero_dates:
        # Hero dates describe one production/location; the opening part of the
        # article normally contains its explicit venue and city.
        location_text = '\n'.join((clean_text(hero), description[:3500]))
        venue, city = location_from_text(location_text)
        if not venue or not city:
            return []
        # Detail copy often contains pre-show talk and doors times which do not
        # describe every date in the hero. Leave the time empty unless an
        # occurrence-specific block supplies it below.
        time_from = None
        for event_date in hero_dates:
            records.append(make_record(title, event_date, url, time_from, venue, city, description))
        return records

    # Series pages sometimes put only the year in the hero and list each
    # concrete performance in separate headings/paragraphs in the article.
    for node in soup.select('main article h2, main article h3, main article p, main article li'):
        block = clean_text(node)
        dates = parse_dates(block, year)
        if not dates:
            continue
        context = block
        sibling = node.find_next_sibling()
        if sibling:
            context += '\n' + clean_text(sibling)
        if re.search(
            r'\b(?:club members?|exclusive experiences?|open rehearsal|technical rehearsal access)\b',
            context,
            re.IGNORECASE,
        ):
            continue
        venue, city = location_from_text(context)
        if not venue or not city:
            continue
        for event_date in dates:
            records.append(
                make_record(title, event_date, url, parse_time(context), venue, city, description)
            )
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'GB',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = performance_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(get_response, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_event(future.result().content, url))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape Northern Ireland Opera performance detail',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda record: (record['date'], record['time_from'] or '', record['title'], record['url']),
    )


class NioperaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='niopera_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='GB',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NioperaComCrawler().run()


if __name__ == '__main__':
    main()
