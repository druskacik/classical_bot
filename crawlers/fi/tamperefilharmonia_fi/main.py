import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.tamperefilharmonia.fi/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Tampere Filharmonia'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}
DATE_RE = re.compile(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b')
TIME_RE = re.compile(r'\b([01]?\d|2[0-3])[.:]([0-5]\d)\b')
POSTAL_CITY_RE = re.compile(r'\b\d{5}\s+([^,]+)')


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def concert_urls(session):
    response = session.get(SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'xml')
    return sorted({
        clean_text(location)
        for location in soup.find_all('loc')
        if '/konsertti/' in clean_text(location)
    })


def info_group(soup, label):
    for group in soup.select('.info-group'):
        heading = clean_text(group.select_one('.info-group__label'))
        if heading.casefold() == label.casefold():
            return group
    return None


def event_occurrences(soup):
    group = info_group(soup, 'Päivät')
    values = []
    if group:
        values = [clean_text(node) for node in group.select('.info-group__description')]

    if not values:
        hero = soup.select_one('.entry__hero-info')
        if hero:
            date_icon = hero.select_one('.icon--date')
            if date_icon and date_icon.parent:
                values = [clean_text(date_icon.parent)]

    occurrences = []
    for value in values:
        dates = DATE_RE.findall(value)
        # Remove calendar dates before looking for a time: otherwise the
        # leading ``17.12`` in ``17.12.2026`` is itself a valid clock shape.
        time_match = TIME_RE.search(DATE_RE.sub('', value))
        time_from = (
            f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            if time_match else None
        )
        for raw_date in dates:
            try:
                event_date = datetime.strptime(raw_date, '%d.%m.%Y').date().isoformat()
            except ValueError:
                continue
            occurrences.append((event_date, time_from))

    if occurrences and all(time_from is None for _, time_from in occurrences):
        hero = soup.select_one('.entry__hero-info')
        time_icon = hero.select_one('.icon--time') if hero else None
        time_match = TIME_RE.search(clean_text(time_icon.parent)) if time_icon and time_icon.parent else None
        if time_match:
            value = f'{int(time_match.group(1)):02d}:{time_match.group(2)}'
            occurrences = [(event_date, value) for event_date, _ in occurrences]

    return list(dict.fromkeys(occurrences))


def location_text(soup):
    group = info_group(soup, 'Sijainti')
    if group:
        value = clean_text(group.select_one('.info-group__description'))
        if value:
            return value
    hero = soup.select_one('.entry__hero-info')
    icon = hero.select_one('.icon--location') if hero else None
    return clean_text(icon.parent) if icon and icon.parent else ''


def parse_location(value):
    if not value:
        return None, None
    city_match = POSTAL_CITY_RE.search(value)
    city = clean_text(city_match.group(1)).splitlines()[0].strip(' .') if city_match else None
    if not city and re.search(r'\bTampere\b', value, re.IGNORECASE):
        city = 'Tampere'

    parts = [part.strip() for part in value.split(',') if part.strip()]
    venue_parts = []
    for part in parts:
        if re.search(r'\d{5}|\d|katu\b|tie\b|kuja\b|väylä\b|aukio\b', part, re.IGNORECASE):
            break
        venue_parts.append(part)
    venue = ', '.join(venue_parts) or (parts[0] if parts else '')
    return venue or None, city


def description_text(soup):
    sections = []
    intro = soup.select_one('main article section:nth-of-type(2) .column.is-8')
    body = soup.select_one(
        'main article section:nth-of-type(2) .column.is-5-desktop.is-offset-1-desktop'
    )
    legacy = soup.select_one('.entry__content')
    for node in (intro, body, legacy):
        value = clean_text(node)
        if value and value not in sections:
            sections.append(value)
    return '\n\n'.join(sections) or None


def parse_concert(soup, url):
    title = clean_text(soup.select_one('h1.entry__title'))
    venue, city = parse_location(location_text(soup))
    if not all((title, venue, city)):
        return []

    description = description_text(soup)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'FI',
            'description': description,
        }
        for event_date, time_from in event_occurrences(soup)
    ]


def scrape_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    urls = concert_urls(session)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_soup, session, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_concert(future.result(), url))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape Tampere Filharmonia concert',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
    return sorted(
        records,
        key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
    )


class TamperefilharmoniaFiCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tamperefilharmonia_fi',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    TamperefilharmoniaFiCrawler().run()


if __name__ == '__main__':
    main()
