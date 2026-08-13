import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.crescendo-jura.ch/'
SOURCE = 'Crescendo Jura'
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-CH,fr;q=0.9',
}

MONTHS = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12,
    'decembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text(' ', strip=True) if hasattr(value, 'get_text') else str(value)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def parse_date(value, fallback_year=None):
    match = re.search(
        r'\b(\d{1,2})\s+'
        r'(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)'
        r'(?:\s+(\d{4}))?',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else fallback_year
    if year is None:
        return None
    month_name = match.group(2).lower()
    try:
        return date(year, MONTHS[month_name], int(match.group(1))).isoformat()
    except (KeyError, ValueError):
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*(?:h|:)\s*([0-5]\d)\b', value, re.IGNORECASE)
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


def record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'CH',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_saint_ursanne(soup, url):
    heading = soup.find(['h1', 'h2'], string=re.compile(r'Programme\s+\d{4}', re.IGNORECASE))
    year_match = re.search(r'\b(20\d{2})\b', clean_text(heading) or url)
    if not year_match:
        return []
    year = int(year_match.group(1))
    records = []
    for item in soup.select('li.item'):
        concert_number = clean_text(item.select_one('.concertNb'))
        # The programme can contain talks and other festival activities alongside concerts.
        if not re.search(r'\bconcert\b', concert_number, re.IGNORECASE):
            continue
        date_text = clean_text(item.select_one('.dateBold'))
        event_date = parse_date(date_text, year)
        title = clean_text(item.select_one('.summary .title')) or concert_number
        description = clean_text(item.select_one('.Contenttogglable .content'))
        if not description:
            description = clean_text(item.select_one('.summaryContent'))
        if not all((title, event_date)):
            continue
        records.append(record(
            title, event_date, url, parse_time(date_text),
            'Cloître de la Collégiale', 'Saint-Ursanne', description,
        ))
    return records


def parse_porrentruy(soup, url):
    records = []
    for card in soup.select('.col-md-4'):
        text = clean_text(card)
        event_date = parse_date(text)
        if not event_date:
            continue
        headings = [clean_text(node) for node in card.find_all(['h2', 'h3', 'h4'])]
        title = next(
            (value for value in headings if value and not parse_date(value) and not parse_time(value)),
            '',
        )
        link = card.find('a', href=True)
        event_url = urljoin(url, link['href']) if link else url
        if not title:
            continue
        description_nodes = card.select('.page-content-htmltext')
        description = clean_text(description_nodes[-1]) if description_nodes else text
        records.append(record(
            title, event_date, event_url, parse_time(text),
            "Salle de l'Inter", 'Porrentruy', description,
        ))
    return records


class CrescendoJuraChCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='crescendo_jura_ch',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CH',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['date', 'time_from', 'venue', 'title'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(SITEMAP_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Crescendo Jura sitemap',
                event='crawler_fetch_failed', level='error', url=SITEMAP_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        sitemap = BeautifulSoup(response.content, 'xml')
        urls = [
            clean_text(node) for node in sitemap.find_all('loc')
            if re.search(r'/Piano-a-(?:Saint-Ursanne|Porrentruy)/Programme-\d{4}/?$', clean_text(node))
        ]
        records = []
        for url in dict.fromkeys(urls):
            try:
                page = session.get(url, timeout=45)
                page.raise_for_status()
                soup = BeautifulSoup(page.text, 'html.parser')
                if '/Piano-a-Saint-Ursanne/' in url:
                    records.extend(parse_saint_ursanne(soup, url))
                else:
                    records.extend(parse_porrentruy(soup, url))
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Crescendo Jura programme',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['venue'], item['title']),
        )


def main():
    CrescendoJuraChCrawler().run()


if __name__ == '__main__':
    main()
