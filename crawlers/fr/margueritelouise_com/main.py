import json
import re
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.margueritelouise.com/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda-ensemble')
SOURCE = 'Ensemble Marguerite Louise'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}
MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def folded(value):
    return ''.join(
        character for character in unicodedata.normalize('NFD', value.lower())
        if unicodedata.category(character) != 'Mn'
    )


DATE_RE = re.compile(
    r'(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?'
    r'(?P<days>\d{1,2}(?:\s*(?:,|et)\s*\d{1,2})*)\s+'
    r'(?P<month>janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|'
    r'septembre|octobre|novembre|d[ée]cembre)\s+(?P<year>20\d{2})',
    re.IGNORECASE,
)
TIME_RE = re.compile(r'(?<!\d)([01]?\d|2[0-3])\s*h\s*([0-5]\d)?', re.IGNORECASE)


def occurrences(text):
    match = DATE_RE.search(text)
    if not match:
        return []
    month = MONTHS[folded(match.group('month'))]
    year = int(match.group('year'))
    time_match = TIME_RE.search(text[match.start():match.end() + 15])
    time_from = None
    if time_match:
        time_from = f'{int(time_match.group(1)):02d}:{time_match.group(2) or "00"}'
    result = []
    for day_text in re.findall(r'\d{1,2}', match.group('days')):
        try:
            result.append((date(year, month, int(day_text)).isoformat(), time_from))
        except ValueError:
            continue
    return result


def location(text):
    """Return only locations stated by, or safely implied from, this calendar."""
    value = folded(text)
    rules = (
        (r'cathedrale de chartes', ('Cathédrale Notre-Dame de Chartres', 'Chartres', 'FR')),
        (r'courtanvaux', ('Château de Courtanvaux', 'Bessé-sur-Braye', 'FR')),
        (r'musicales du clocher|saint astier', ('Festival les Musicales du clocher', 'Saint-Astier', 'FR')),
        (r'chapelle royale(?: de versailles)?', ('Chapelle Royale', 'Versailles', 'FR')),
        (r'sinfonia smith square', ('Sinfonia Smith Square', 'London', 'GB')),
        (r'notre-dame de l.assomption a champcueil', ("Église Notre-Dame-de-l'Assomption", 'Champcueil', 'FR')),
        (r'chapelle de la trinite.*fontainebleau', ('Chapelle de la Trinité, Château de Fontainebleau', 'Fontainebleau', 'FR')),
        (r'chapelle notre-dame de reconciliation', ('Chapelle Notre-Dame de Réconciliation', 'Lille', 'FR')),
        (r'cathedrale de porto', ('Cathédrale de Porto', 'Porto', 'PT')),
        (r'cathedrale de braga', ('Cathédrale de Braga', 'Braga', 'PT')),
        (r'cathedrale de lisbonne', ('Cathédrale de Lisbonne', 'Lisbon', 'PT')),
        (r'chapelle de la misericorde', ('Chapelle de la Miséricorde', 'Monaco', 'MC')),
        (r'musikfestspiele potsdam', ('Musikfestspiele Potsdam Sanssouci', 'Potsdam', 'DE')),
        (r'cathedrale de varsovie', ('Cathédrale de Varsovie', 'Warsaw', 'PL')),
        (r'eglise saint-nicolas de gdansk', ('Église Saint-Nicolas', 'Gdańsk', 'PL')),
        (r'eglise de la ferte-milon', ('Église de La Ferté-Milon', 'La Ferté-Milon', 'FR')),
        (r'academie equestre de versailles', ('Académie Équestre de Versailles', 'Versailles', 'FR')),
        (r'festival de lanvellec', ('Festival de Lanvellec', 'Lanvellec', 'FR')),
        (r'saint michel en thierache', ('Abbaye de Saint-Michel-en-Thiérache', 'Saint-Michel', 'FR')),
    )
    for pattern, result in rules:
        if re.search(pattern, value, re.DOTALL):
            return result
    return None


def detail_description(session, url):
    if urlparse(url).netloc != urlparse(SOURCE_URL).netloc:
        return None
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Marguerite Louise event detail',
            event='crawler_detail_fetch_failed', level='warning', url=url,
            error_type=type(error).__name__, error_message=str(error),
        )
        return None
    main = BeautifulSoup(response.text, 'html.parser').find('main')
    return clean_text(main) or None


def upcoming_records(session, soup):
    records = []
    seen_containers = set()
    for node in soup.select('[data-testid="inline-content"]'):
        text = clean_text(node)
        if 'Découvrir' not in text or not occurrences(text):
            continue
        # Nested Wix wrappers can expose the same card more than once.
        signature = (text, node.get('data-mesh-id'))
        if signature in seen_containers:
            continue
        seen_containers.add(signature)
        lines = [line for line in text.splitlines() if line and line != '\u200b']
        date_index = next((index for index, line in enumerate(lines) if DATE_RE.search(line)), None)
        if date_index is None:
            continue
        title = clean_text('\n'.join(lines[:date_index])).strip(' .')
        title = re.sub(r'^Découvrir\s*', '', title, flags=re.IGNORECASE)
        if re.search(r'prochains rendez-vous|barok music by', title, re.IGNORECASE):
            continue
        place = location(text)
        if not title or not place:
            continue
        links = [urljoin(AGENDA_URL, link.get('href')) for link in node.select('a[href]')]
        url = next((link for link in links if link and not link.startswith('mailto:')), AGENDA_URL)
        description = detail_description(session, url) or text
        venue, city, country_code = place
        for event_date, time_from in occurrences(text):
            records.append(make_record(title, event_date, time_from, url, venue, city, country_code, description))
    return records


def gallery_items(page_html):
    marker_re = re.compile(r'"[^"]+_galleryData":')
    decoder = json.JSONDecoder()
    items = []
    for match in marker_re.finditer(page_html):
        try:
            data, _ = decoder.raw_decode(page_html[match.end():])
        except json.JSONDecodeError:
            continue
        items.extend(data.get('items', []))
    return items


def archive_records(page_html):
    records = []
    for item in gallery_items(page_html):
        metadata = item.get('metaData') or {}
        title = clean_text(metadata.get('title'))
        description = clean_text(metadata.get('description'))
        combined = f'{title}\n{description}'
        if not title or not description or re.search(r'enregistrement|sortie du disque', folded(combined)):
            continue
        place = location(combined)
        if not place:
            continue
        venue, city, country_code = place
        item_id = item.get('itemId')
        url = f'{AGENDA_URL}?pgid=mi1llpgg-{item_id}' if item_id else AGENDA_URL
        for event_date, time_from in occurrences(description):
            records.append(make_record(title, event_date, time_from, url, venue, city, country_code, description))
    return records


def make_record(title, event_date, time_from, url, venue, city, country_code, description):
    return {
        'title': title, 'date': event_date, 'url': url, 'time_from': time_from,
        'venue': venue, 'city': city, 'country_code': country_code,
        'description': description or None, 'source_url': SOURCE_URL, 'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        response = session.get(AGENDA_URL, timeout=60)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Marguerite Louise agenda',
            event='crawler_fetch_failed', level='error', url=AGENDA_URL,
            error_type=type(error).__name__, error_message=str(error),
        )
        raise
    soup = BeautifulSoup(response.text, 'html.parser')
    records = upcoming_records(session, soup) + archive_records(response.text)
    unique = {(r['title'], r['date'], r['time_from'], r['venue']): r for r in records}
    return sorted(unique.values(), key=lambda r: (r['date'], r['time_from'] or '', r['title']))


class MargueriteLouiseComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='margueritelouise_com', source=SOURCE, source_url=SOURCE_URL,
        country_code='FR', upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    MargueriteLouiseComCrawler().run()


if __name__ == '__main__':
    main()
