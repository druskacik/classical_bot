import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://belleilemusique.com/'
SOURCE = 'Plage musicale à Belle-Île'
PROGRAM_URL = f'{SOURCE_URL}festival/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
}

# Several island venues are printed without their municipality in the programme.
VENUE_CITIES = {
    'auberge de jeunesse': 'Le Palais',
    'bibliotheque du genie': 'Le Palais',
    'eglise de palais': 'Le Palais',
    'eglise de le palais': 'Le Palais',
    'eglise de bangor': 'Bangor',
    'eglise de locmaria': 'Locmaria',
    'fort du bugull': 'Locmaria',
    'fort sarah bernhardt': 'Sauzon',
    'jardin la boulaye': 'Locmaria',
    'le grand phare': 'Bangor',
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    text = clean_text(value).casefold()
    return ''.join(
        character for character in unicodedata.normalize('NFKD', text)
        if not unicodedata.combining(character)
    )


def programme_year(soup):
    for link in soup.select('a[href]'):
        match = re.search(r'programme_plage_musicale_(\d{4})\.pdf', link.get('href', ''))
        if match:
            return int(match.group(1))
    return None


def parse_schedule(value, year):
    text = normalized(value)
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\b', text
    )
    if not match:
        return None
    try:
        event_date = date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None

    time_match = re.search(r'\b(\d{1,2})h(?:(\d{2}))?', text)
    time_from = None
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def resolve_place(value, title, description):
    venue_text = clean_text(value)
    if not venue_text and 'croisiere musicale' in normalized(title):
        return 'Église Saint-Gildas', 'Houat'
    if not venue_text:
        return None, None

    if ',' in venue_text:
        venue, locality = [part.strip() for part in venue_text.rsplit(',', 1)]
        city_key = normalized(locality)
        if city_key == 'kervilahouen':
            return venue, 'Bangor'
        if city_key in {'le palais', 'bangor', 'locmaria', 'sauzon'}:
            return venue, clean_text(locality)

    key = normalized(venue_text)
    for venue_key, city in VENUE_CITIES.items():
        if key.startswith(venue_key):
            return venue_text, city

    # The programme occasionally places the locality only in prose.
    prose = normalized(description)
    for city in ('Le Palais', 'Bangor', 'Locmaria', 'Sauzon', 'Houat'):
        if normalized(city) in prose:
            return venue_text, city
    return None, None


def programme_list(soup):
    heading = next(
        (node for node in soup.find_all(['h2', 'h3'])
         if 'programme des concerts' in normalized(node)),
        None,
    )
    return heading.find_next('ol') if heading else None


def choir_details(soup):
    details = {}
    for heading in soup.find_all('h4'):
        key = normalized(heading)
        if not key.startswith('concert '):
            continue
        item = heading.find_parent('li')
        works = [clean_text(node) for node in item.select('li')] if item else []
        text = '\n'.join(work for work in works if work)
        if 'pavarotti' in key:
            details['hommage a pavarotti'] = text
        elif 'bach' in key:
            details['bach'] = text
        elif 'vivaldi' in key:
            details['vivaldi'] = text
    return details


def parse_programme(html):
    soup = BeautifulSoup(html, 'html.parser')
    year = programme_year(soup)
    event_list = programme_list(soup)
    if not year or not event_list:
        return []
    enrichments = choir_details(soup)
    records = []

    for item in event_list.find_all('li', recursive=False):
        title = clean_text(item.find('h4'))
        first_div = item.find('div', recursive=False)
        schedule_node = first_div.find('span', recursive=False) if first_div else None
        parsed = parse_schedule(schedule_node, year)
        description_node = item.find('p', class_=lambda value: value and 'font-medium' in value)
        description = clean_text(description_node) or None
        if not title or not parsed:
            continue

        title_key = normalized(title)
        for keyword, detail in enrichments.items():
            if keyword in title_key and detail:
                description = '\n\n'.join(part for part in (description, detail) if part)
                break

        venue_node = first_div.find('span', class_=lambda value: value and 'italic' in value) if first_div else None
        venue, city = resolve_place(venue_node, title, description)
        if not venue or not city:
            log_message(
                'Skipping festival item without defensible geography',
                event='crawler_item_skipped',
                level='warning',
                url=PROGRAM_URL,
                title=title,
            )
            continue
        event_date, time_from = parsed
        records.append({
            'title': title,
            'date': event_date,
            'url': PROGRAM_URL,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class BelleIleMusiqueComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='belleilemusique_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(PROGRAM_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        return parse_programme(response.content)


def main():
    BelleIleMusiqueComCrawler().run()


if __name__ == '__main__':
    main()
