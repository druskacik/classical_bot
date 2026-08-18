import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.septembre-musical.com/'
CONCERTS_URL = f'{SOURCE_URL}programmation/concerts/'
SOURCE = "Festival Septembre musical de l'Orne"

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1,
    'février': 2,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'août': 8,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'décembre': 12,
    'decembre': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'\b(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(20\d{2})\b', value, re.IGNORECASE
    )
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)\b', value, re.IGNORECASE)
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_location(value):
    parts = [part.strip() for part in re.split(r'\s*[•·]\s*', value) if part.strip()]
    if len(parts) < 2:
        return None
    venue = parts[0]
    city = parts[-1]
    return (venue, city) if venue and city and venue != city else None


def parse_listing_article(article):
    title_link = article.select_one('.elementor-post__title a[href]')
    title = clean_text(title_link)
    url = title_link.get('href', '').strip() if title_link else ''
    event_date = parse_date(clean_text(article.select_one('.post__custom-date')))
    location = parse_location(clean_text(article.select_one('.post__custom-location')))
    if not title or not url or not event_date or not location:
        return None

    venue, city = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(clean_text(article.select_one('.post__custom-time'))),
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': clean_text(article.select_one('.elementor-post__excerpt')) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def detail_description(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    presentation = soup.select_one('.elementor-widget-cc_spectacle_presentation')
    if presentation is None:
        return None

    sections = []
    body = presentation.select_one('.concert-details__description')
    if body:
        sections.append(clean_text(body))
    else:
        paragraphs = [clean_text(item) for item in presentation.select('p.p1')]
        sections.extend(text for text in paragraphs if text)

    for selector in ('.member__container', '.music-info__container'):
        section = clean_text(presentation.select_one(selector))
        if section:
            sections.append(section)

    unique_sections = list(dict.fromkeys(section for section in sections if section))
    return '\n\n'.join(unique_sections) or None


class SeptembreMusicalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='septembre_musical_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            response = session.get(CONCERTS_URL, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Septembre musical concert listing',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for article in soup.select('article.elementor-post.product_cat-concerts'):
            record = parse_listing_article(article)
            if record:
                records.append(record)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(detail_description, session, record['url']): record
                for record in records
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    record['description'] = future.result() or record['description']
                except requests.RequestException as error:
                    log_message(
                        'Failed to fetch Septembre musical concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=record['url'],
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SeptembreMusicalComCrawler().run()


if __name__ == '__main__':
    main()
