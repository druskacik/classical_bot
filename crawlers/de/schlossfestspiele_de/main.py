import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://schlossfestspiele.de/'
ARCHIVE_URL = urljoin(SOURCE_URL, 'en/programme-archive/')
LEGACY_ARCHIVE_URL = 'https://archiv.schlossfestspiele.de/produktionsarchiv'
SOURCE = 'Ludwigsburger Schlossfestspiele'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9,de;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=Retry(total=3, backoff_factor=0.7,
                          status_forcelist=(429, 500, 502, 503, 504)),
    ))
    return session


def get_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def split_location(value, default_city='Ludwigsburg'):
    value = clean_text(value)
    if not value:
        return None, None
    if ',' in value:
        venue, city = (part.strip() for part in value.rsplit(',', 1))
        if venue and city:
            city = re.sub(r'^\d{5}\s+', '', city)
            city = re.sub(r'\s+', ' ', city).strip()
            if 'Ludwigsburg' in city:
                city = 'Ludwigsburg'
            elif city == 'Baden-Württemberg':
                return None, None
            elif city.startswith('Wolfegg'):
                city = 'Wolfegg'
            return venue, city
    # Touring entries on the new calendar sometimes put the town in the
    # venue field. Such a town is not a defensible venue and is skipped later.
    cities = ('Haigerloch', 'Wolfegg', 'Stuttgart', 'Heilbronn', 'Bietigheim-Bissingen')
    for city in cities:
        if city.casefold() in value.casefold():
            return value, city
    return value, default_city


def detail_description(soup):
    parts = []
    for node in soup.select('#av_section_2 .avia_textblock, #av_section_3 .avia_textblock'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def detail_venue(soup):
    link = soup.select_one('a.spielstaette-link')
    if link:
        return clean_text(link)
    for heading in soup.find_all(['h2', 'h3', 'h4']):
        if clean_text(heading).casefold() in ('venue', 'spielstätte'):
            parent = heading.parent
            heading.extract()
            return clean_text(parent)
    return None


def parse_modern_card(card, year, detail_soup=None):
    title = clean_text(card.select_one('.event-name'))
    date_text = clean_text(card.select_one('.date'))
    url = urljoin(ARCHIVE_URL, card.get('href', ''))
    venue_text = detail_venue(detail_soup) if detail_soup else None
    venue_text = venue_text or card.get('data-venue', '')
    venue, city = split_location(venue_text)
    match = re.search(
        r'(\d{1,2})\.\s+([A-Za-z]+)[\s\S]*?(\d{1,2})\s+o.clock', date_text
    )
    if not (title and url and venue and city and match):
        return None
    # A bare town is location information, not a venue name.
    if venue.casefold() == city.casefold():
        return None
    try:
        moment = datetime.strptime(
            f'{match.group(1)} {match.group(2)} {year} {match.group(3)}',
            '%d %B %Y %H',
        )
    except ValueError:
        return None
    description = detail_description(detail_soup) if detail_soup else None
    subtitle = clean_text(card.select_one('.subheadline'))
    if subtitle and (not description or subtitle not in description):
        description = '\n\n'.join(part for part in (subtitle, description) if part)
    return {
        'title': title, 'date': moment.date().isoformat(), 'url': url,
        'time_from': moment.strftime('%H:%M'), 'venue': venue, 'city': city,
        'country_code': 'DE', 'description': description,
        'source_url': SOURCE_URL, 'source': SOURCE,
    }


def modern_cards(soup):
    cards = []
    for pane in soup.select('.archive-year[data-year]'):
        year = pane.get('data-year', '')
        if re.fullmatch(r'20\d{2}', year):
            cards.extend((card, int(year)) for card in pane.select('a.event-item[href]'))
    return cards


def legacy_links(soup):
    return sorted({urljoin(LEGACY_ARCHIVE_URL, link.get('href', ''))
                   for link in soup.select('a.productionoverviewlink[href]')})


def parse_legacy_detail(soup, url):
    title = clean_text(soup.title).split('|', 1)[0].strip() if soup.title else ''
    description = '\n\n'.join(filter(None, (
        clean_text(soup.select_one('.productiondescsidebox')),
        clean_text(soup.select_one('.productiondescmainbox')),
    ))) or None
    records = []
    for row in soup.select('.productioncalsectionel[dp-datestr]'):
        try:
            date = datetime.strptime(row['dp-datestr'], '%d.%m.%Y').date().isoformat()
        except (KeyError, ValueError):
            continue
        time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', clean_text(row.select_one('.dateinfo')))
        location = clean_text(row.select_one('.placeinfo'))
        venue, city = split_location(location)
        if not (title and venue and city) or venue.casefold() == city.casefold():
            continue
        records.append({
            'title': title, 'date': date, 'url': url,
            'time_from': time_match.group(0) if time_match else None,
            'venue': venue, 'city': city, 'country_code': 'DE',
            'description': description, 'source_url': SOURCE_URL, 'source': SOURCE,
        })
    return records


def get_concerts():
    session = make_session()
    modern_soup = get_soup(session, ARCHIVE_URL)
    cards = modern_cards(modern_soup)
    records = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, urljoin(ARCHIVE_URL, card['href'])):
                   (card, year) for card, year in cards}
        for future in as_completed(futures):
            card, year = futures[future]
            try:
                record = parse_modern_card(card, year, future.result())
                if record:
                    records.append(record)
            except requests.RequestException as error:
                log_message('Failed to scrape Schlossfestspiele event detail',
                            event='crawler_item_failed', level='warning',
                            url=urljoin(ARCHIVE_URL, card.get('href', '')),
                            error_type=type(error).__name__, error_message=str(error))
                record = parse_modern_card(card, year)
                if record:
                    records.append(record)

    legacy_soup = get_soup(session, LEGACY_ARCHIVE_URL)
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(get_soup, session, url): url
                   for url in legacy_links(legacy_soup)}
        for future in as_completed(futures):
            url = futures[future]
            try:
                records.extend(parse_legacy_detail(future.result(), url))
            except requests.RequestException as error:
                log_message('Failed to scrape Schlossfestspiele legacy event',
                            event='crawler_item_failed', level='warning', url=url,
                            error_type=type(error).__name__, error_message=str(error))

    unique = {(r['url'], r['date'], r['time_from'], r['venue']): r for r in records}
    return sorted(unique.values(), key=lambda r: (
        r['date'], r['time_from'] or '', r['city'], r['title'], r['url']))


class SchlossfestspieleDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='schlossfestspiele_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    SchlossfestspieleDeCrawler().run()


if __name__ == '__main__':
    main()
