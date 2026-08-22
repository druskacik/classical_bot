import re
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mikkoluoma.com/'
# HTTPS currently has a hostname-mismatched certificate; the same first-party
# document is served successfully by the site's HTTP endpoint.
CONCERTS_URL = 'http://mikkoluoma.com/concerts.html'
SOURCE = 'Mikko Luoma'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fi-FI,fi;q=0.9,en;q=0.7',
}

# The archive contains Mikko Luoma's international appearances.  The site does
# not expose structured geography, so these unambiguous place names turn its
# free-text location field into explicit city/country values.
PLACES = (
    (('washington dc',), 'Washington, D.C.', 'US'),
    (('new york',), 'New York', 'US'),
    (('freiburg',), 'Freiburg im Breisgau', 'DE'),
    (('essen',), 'Essen', 'DE'),
    (('wandlitz',), 'Wandlitz', 'DE'),
    (('berlin',), 'Berlin', 'DE'),
    (('reykjavik',), 'Reykjavík', 'IS'),
    (('prague', 'rudolfinum'), 'Prague', 'CZ'),
    (('tallinn',), 'Tallinn', 'EE'),
    (('stockholm',), 'Stockholm', 'SE'),
    (('wejherowo',), 'Wejherowo', 'PL'),
    (('paris',), 'Paris', 'FR'),
    (('helsinki',), 'Helsinki', 'FI'),
    (('turku', 'turun '), 'Turku', 'FI'),
    (('tampere',), 'Tampere', 'FI'),
    (('kokkola',), 'Kokkola', 'FI'),
    (('virrat',), 'Virrat', 'FI'),
    (('nauvo', 'seili'), 'Nauvo', 'FI'),
    (('salo',), 'Salo', 'FI'),
    (('jyväskylä',), 'Jyväskylä', 'FI'),
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_soup():
    try:
        response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=60)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to read Mikko Luoma concert archive',
            event='crawler_item_failed',
            level='error',
            url=CONCERTS_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise
    # The server incorrectly declares ISO-8859-1; the document is UTF-8.
    response.encoding = 'utf-8'
    return BeautifulSoup(response.text, 'html.parser')


def parse_date_time(value):
    date_match = re.search(r'\b(\d{1,2}\.\d{1,2}\.\d{4})\b', value)
    if not date_match:
        return None, None
    try:
        event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date().isoformat()
    except ValueError:
        return None, None
    time_match = re.search(r'\bklo\s+([01]?\d|2[0-3]):([0-5]\d)\b', value, re.I)
    time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2)}' if time_match else None
    return event_date, time_from


def location_text(details):
    for term in details.find_all('dt', recursive=False):
        if clean_text(term).casefold() == 'paikka':
            value = term.find_next_sibling('dd')
            return clean_text(value)
    return ''


def parse_geography(title, location):
    evidence = f'{title}\n{location}'.casefold()
    city = country_code = None
    for needles, candidate_city, candidate_country in PLACES:
        if any(needle in evidence for needle in needles):
            city, country_code = candidate_city, candidate_country
            break

    if not city or location.casefold().strip() == 'see above':
        return None, None, None

    lines = [part.strip(' ,;.') for part in location.split('\n') if part.strip(' ,;.')]
    if not lines:
        return None, None, None

    venue = lines[0]
    if 'concert hall of the kokkola' in location.casefold():
        venue = next(part for part in lines if 'concert hall' in part.casefold())
    venue = re.split(r',?\s+during\b', venue, maxsplit=1, flags=re.I)[0].strip(' ,;.')
    venue = re.sub(
        r',\s*(?:Turku(?: Music Festival)?|Turun musiikkijuhlat.*|Tampere|Finland|Poland)$',
        '',
        venue,
        flags=re.I,
    ).strip(' ,;.')
    venue = re.sub(r',\s*Linnankatu\b.*$', '', venue, flags=re.I).strip(' ,;.')
    venue = re.sub(
        r',\s*(?:Tallinn|Stockholm|Turku|Helsinki|Paris|Berlin|New York)(?:,\s*\w+)?$',
        '',
        venue,
        flags=re.I,
    ).strip(' ,;.')

    # A bare city/locality is not a defensible venue.
    locality_only = {
        'salo', 'jyväskylä', 'berlin, wannsee', 'berlin, wandlitz',
        'nordic music days, reykjavik',
    }
    if location.casefold().strip() in locality_only:
        return None, None, None
    if venue.casefold() == city.casefold():
        return None, None, None
    return venue or None, city, country_code


def description_text(details):
    parts = []
    excluded = {'paikka', 'pääsylipputiedot', 'tiedustelut:'}
    for child in details.find_all(['dt', 'dd'], recursive=False):
        text = clean_text(child)
        if not text:
            continue
        if child.name == 'dt' and text.casefold() in excluded:
            continue
        previous = child.find_previous_sibling('dt') if child.name == 'dd' else None
        if previous and clean_text(previous).casefold() in excluded:
            continue
        parts.append(text)
    return '\n'.join(parts) or None


def parse_event(heading):
    date_node = heading.find_next_sibling('p')
    details = date_node.find_next_sibling('dl') if date_node else None
    if not date_node or not details:
        return None

    title = clean_text(heading)
    event_date, time_from = parse_date_time(clean_text(date_node))
    venue, city, country_code = parse_geography(title, location_text(details))
    if not all((title, event_date, venue, city, country_code)):
        return None

    anchor = quote(title, safe='')
    return {
        'title': title,
        'date': event_date,
        'url': f'{CONCERTS_URL}#{anchor}',
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description_text(details),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MikkoluomaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mikkoluoma_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FI',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = [parse_event(heading) for heading in get_soup().select('#content h2')]
        return sorted(
            (record for record in records if record),
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    MikkoluomaComCrawler().run()


if __name__ == '__main__':
    main()
