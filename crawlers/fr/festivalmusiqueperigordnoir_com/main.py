import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://festivalmusiqueperigordnoir.com/'
SOURCE = 'Festival du Périgord Noir'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
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
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return '\n'.join(re.sub(r'\s+', ' ', line).strip() for line in text.splitlines() if line.strip())


def folded(value):
    text = unicodedata.normalize('NFKD', clean_text(value).casefold())
    return ''.join(char for char in text if not unicodedata.combining(char))


def parse_date(value):
    match = re.search(r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b', value)
    if not match:
        return None
    month = MONTHS.get(folded(match.group(2)))
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.fullmatch(r'([01]?\d|2[0-3])\s*h\s*([0-5]\d)?', folded(value))
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}' if match else None


def infer_city(venue):
    text = folded(venue)
    places = {
        'saint-amand-de-coly': 'Saint-Amand-de-Coly',
        'ajat': 'Ajat',
        'fanlac': 'Fanlac',
        'sarlat': 'Sarlat-la-Canéda',
        'hautefort': 'Hautefort',
        'montignac-lascaux': 'Montignac-Lascaux',
        'terrasson-lavilledieu': 'Terrasson-Lavilledieu',
        'terrason-lavilledieu': 'Terrasson-Lavilledieu',
        'saint-leon-sur-vezere': 'Saint-Léon-sur-Vézère',
    }
    for needle, city in places.items():
        if needle in text:
            return city
    return None


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=('GET',),
    )))
    return session


def detail_description(session, url):
    try:
        response = session.get(url, timeout=45)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Concert detail request failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('main .page-content') or soup.select_one('main')
    return clean_text(content) or None


def parse_card(card, url, description):
    lines = clean_text(card).splitlines()
    date_index = next((i for i, line in enumerate(lines) if parse_date(line)), None)
    if date_index is None:
        return None
    time_index = next((i for i in range(date_index + 1, len(lines)) if parse_time(lines[i])), None)
    if time_index is None or time_index <= date_index + 1:
        return None
    venue = lines[date_index + 1]
    city = infer_city(venue)
    title = next((line for line in lines[time_index + 1:] if folded(line) not in {
        'en savoir plus', 'reservez', 'gratuit',
    }), '')
    event_date = parse_date(lines[date_index])
    if not all((title, event_date, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(lines[time_index]),
        'venue': venue,
        'city': city,
        'description': description,
    }


def parse_mini_concerts(card, url, description, year):
    text = clean_text(card)
    venue_match = re.search(r'(Devant l[’\' ]Abbaye de Saint-Amand-de-Coly)', text, re.I)
    if not venue_match:
        return []
    venue = venue_match.group(1)
    records = []
    for day, month_name, hour, minute in re.findall(
        r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+([01]?\d|2[0-3])h([0-5]\d)?\b', text
    ):
        month = MONTHS.get(folded(month_name))
        if not month:
            continue
        try:
            event_date = date(year, month, int(day)).isoformat()
        except ValueError:
            continue
        records.append({
            'title': 'Les Minis Concerts de l’Ensemble Baroque du Périgord Noir',
            'date': event_date,
            'url': url,
            'time_from': f'{int(hour):02d}:{int(minute or 0):02d}',
            'venue': venue,
            'city': 'Saint-Amand-de-Coly',
            'description': description,
        })
    return records


class FestivalMusiquePerigordNoirCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='festivalmusiqueperigordnoir_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = make_session()
        response = session.get(SOURCE_URL, timeout=45)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        seen_urls = set()
        years = [int(value) for value in re.findall(r'\b20\d{2}\b', clean_text(soup))]
        season_year = max(years) if years else date.today().year
        for link in soup.find_all('a', href=True):
            url = link['href'].strip()
            if '/concerts/' not in url or url in seen_urls:
                continue
            seen_urls.add(url)
            card = link.find_parent(class_='elementor-widget-wrap')
            if not card:
                continue
            description = detail_description(session, url)
            if url.rstrip('/').endswith('/les-minis-concerts'):
                records.extend(parse_mini_concerts(card, url, description, season_year))
                continue
            record = parse_card(card, url, description)
            if record:
                records.append(record)
        log_message(
            'Festival agenda scraped',
            level='info',
            url=SOURCE_URL,
            record_count=len(records),
        )
        return records


def main():
    return FestivalMusiquePerigordNoirCrawler().run()


if __name__ == '__main__':
    main()
