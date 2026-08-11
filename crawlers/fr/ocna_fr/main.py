import re
import unicodedata
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.ocna.fr/'
SOURCE = 'Orchestre de Chambre Nouvelle-Aquitaine'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}
MONTHS = {
    'jan': 1, 'janvier': 1,
    'fev': 2, 'fevr': 2, 'fevrier': 2,
    'mars': 3,
    'avr': 4, 'avril': 4,
    'mai': 5,
    'juin': 6,
    'juil': 7, 'juillet': 7,
    'aou': 8, 'aout': 8,
    'sep': 9, 'sept': 9, 'septembre': 9,
    'oct': 10, 'octobre': 10,
    'nov': 11, 'novembre': 11,
    'dec': 12, 'decembre': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalized(value):
    return ''.join(
        character for character in unicodedata.normalize('NFKD', clean_text(value).lower())
        if not unicodedata.combining(character)
    )


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def season_years(soup):
    heading = next(
        (clean_text(item) for item in soup.select('.home-saison-wrapper, #saison, .heading')
         if re.search(r'20\d{2}\s*[–-]\s*(?:20)?\d{2}', clean_text(item))),
        '',
    )
    match = re.search(r'(20\d{2})\s*[–-]\s*(?:20)?(\d{2,4})', heading)
    if not match:
        raise ValueError('Could not determine the OCNA season years')
    start_year = int(match.group(1))
    end_year = int(match.group(2))
    if end_year < 100:
        end_year += (start_year // 100) * 100
    return start_year, end_year


def parse_date(day, month_name, start_year, end_year):
    month_key = normalized(month_name).rstrip('.')
    month = MONTHS.get(month_key) or MONTHS.get(month_key[:4]) or MONTHS.get(month_key[:3])
    if not month or not re.fullmatch(r'\d{1,2}', clean_text(day)):
        return None
    year = start_year if month >= 7 else end_year
    try:
        return date(year, month, int(clean_text(day))).isoformat()
    except ValueError:
        return None


def detail_description(soup):
    parts = []
    programme = clean_text(soup.select_one('.prog-artistique-wrapper'))
    if programme:
        parts.append('Programme\n' + programme)
    artists = clean_text(soup.select_one('.prog-chefs-et-solistes-wrapper'))
    if artists:
        parts.append('Artistes\n' + artists)
    body = clean_text(soup.select_one('.prog-text-wrapper .texte-courant'))
    if body:
        parts.append(body)
    return '\n\n'.join(parts) or None


def parse_detail(html, url, start_year, end_year):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('h1.titre-h1.programmes'))
    description = detail_description(soup)
    dates = soup.select('.prog-dates-wrapper')
    locations = soup.select('.prog-dates-et-lieux-wrapper')
    records = []
    for date_block, location_block in zip(dates, locations):
        day = clean_text(date_block.select_one('.prog-date-jour'))
        month = clean_text(date_block.select_one('.prog-date-mois'))
        event_date = parse_date(day, month, start_year, end_year)
        time_from = clean_text(date_block.select_one('.prog-date-heure')) or None
        city = clean_text(location_block.select_one('.prog-dates-ville'))
        venue = clean_text(location_block.select_one('.prog-dates-lieu'))
        if time_from and not re.fullmatch(r'[0-2]\d:[0-5]\d', time_from):
            time_from = None
        if not title or not event_date or not city or not venue:
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def parse_agenda(soup, start_year, end_year):
    records = []
    for item in soup.select('.agenda-item'):
        title = clean_text(item.select_one('.home-cal'))
        day = clean_text(item.select_one('.home-cal-date-jour'))
        month = clean_text(item.select_one('.home-cal-date-mois'))
        event_date = parse_date(day, month, start_year, end_year)
        location = clean_text(item.select_one('.home-cal-lieu'))
        city, separator, venue = location.partition(',')
        city, venue = city.strip(), venue.strip() if separator else ''
        link = item.select_one('a[href]')
        href = link.get('href') if link else ''
        url = canonical_url(href) if href and href != '#' else SOURCE_URL
        description = clean_text(item.select_one('.home-cal-compositeur')) or None
        if not title or not event_date or not city or not venue:
            log_message(
                'Skipped incomplete OCNA agenda item',
                event='crawler_item_skipped',
                level='warning',
                url=url,
                error_type='IncompleteEventData',
                error_message='Required title, date, city, or venue is missing',
            )
            continue
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': None,
            'venue': venue,
            'city': city,
            'country_code': 'FR',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class OcnaFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='ocna_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
        response.raise_for_status()
        homepage = BeautifulSoup(response.text, 'html.parser')
        start_year, end_year = season_years(homepage)

        programme_urls = sorted({
            canonical_url(link.get('href'))
            for link in homepage.select('.tab-pane-concerts a[href*="/programmes/"]')
        })
        records = []
        for url in programme_urls:
            try:
                detail_response = requests.get(url, headers=HEADERS, timeout=45)
                detail_response.raise_for_status()
                records.extend(parse_detail(detail_response.text, url, start_year, end_year))
            except requests.RequestException as error:
                log_message(
                    'Failed to scrape OCNA programme',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )

        detail_keys = {
            (record['date'], normalized(record['city']), normalized(record['venue']))
            for record in records
        }
        for record in parse_agenda(homepage, start_year, end_year):
            key = (record['date'], normalized(record['city']), normalized(record['venue']))
            if key not in detail_keys:
                records.append(record)

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['city'], item['title']),
        )


def main():
    OcnaFrCrawler().run()


if __name__ == '__main__':
    main()
