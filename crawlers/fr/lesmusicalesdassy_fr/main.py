import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lesmusicalesdassy.fr/'
SOURCE = "Les Musicales d'Assy"
PAGES_SITEMAP_URL = f'{SOURCE_URL}pages-sitemap.xml'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

MONTHS = {
    'janvier': 1,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'decembre': 12,
}

# These are the three performance sites used by the festival. Doran is on the
# neighbouring commune of Sallanches; the other two are in Passy.
VENUES = {
    'eglise notre dame de toute grace': ('Église Notre-Dame-de-Toute-Grâce', 'Passy'),
    'eglise notre dame toute grace': ('Église Notre-Dame-de-Toute-Grâce', 'Passy'),
    'chapelle de doran': ('Chapelle de Doran', 'Sallanches'),
    'jardin des cimes': ('Jardin des Cimes', 'Passy'),
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text(' ', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    return re.sub(r'\s+', ' ', text).strip()


def normalized(value):
    value = clean_text(value).casefold()
    return ''.join(
        character for character in unicodedata.normalize('NFKD', value)
        if not unicodedata.combining(character)
    )


def page_urls(session):
    response = session.get(PAGES_SITEMAP_URL, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'xml')
    return {
        clean_text(node)
        for node in soup.select('url > loc')
        if clean_text(node).startswith(SOURCE_URL)
    }


def parse_date_time(value, year):
    text = normalized(value)
    match = re.search(
        r'\b(\d{1,2}|1er)\s+(' + '|'.join(MONTHS) + r')\b(?:\s+(\d{4}))?'
        r'(?:\s*[-,]\s*(\d{1,2})(?:\s*[h:])\s*(\d{2})?)?',
        text,
    )
    if not match:
        return None
    day = 1 if match.group(1) == '1er' else int(match.group(1))
    event_year = int(match.group(3) or year)
    try:
        event_date = date(event_year, MONTHS[match.group(2)], day).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5) or 0)
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from


def detail_record(session, url):
    response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')

    # Wix pages use h4 for event titles and h6 for the performance date. Some
    # programmes put repertoire headings before the date, so all preceding h4
    # elements are retained as the title.
    date_node = next(
        (node for node in soup.select('h4, h5, h6, p') if re.search(r'\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)\b', normalized(node))),
        None,
    )
    if not date_node:
        return None

    page_title = clean_text(soup.title)
    year_match = re.search(r'\b(20\d{2})\b', page_title)
    if not year_match:
        return None
    performance = parse_date_time(date_node, int(year_match.group(1)))
    if not performance:
        return None

    headings = []
    for node in soup.select('h4'):
        if date_node in node.find_all_next():
            text = clean_text(node)
            if text and text not in headings:
                headings.append(text)
    if not headings:
        previous = date_node.find_previous('p')
        if previous:
            headings.append(clean_text(previous))
    title = ' '.join(headings)
    title = re.sub(r'\bJ\s+eunes\b', 'Jeunes', title)
    title = re.sub(r'\s+"$', '"', title).strip()
    if not title or normalized(title).startswith('master class'):
        return None

    venue = city = None
    for node in date_node.find_all_next(['p', 'h4', 'h5', 'h6']):
        key = normalized(node)
        if key in VENUES:
            venue, city = VENUES[key]
            break
    if not venue:
        return None

    content = []
    started = False
    for node in soup.select('h4, h5, h6, p'):
        text = clean_text(node)
        if node is date_node or text == headings[0]:
            started = True
        if not started or not text:
            continue
        if re.match(r'^(tarif|pass festival|r[eé]server|s.inscrire)', normalized(text)):
            continue
        if text not in content:
            content.append(text)

    event_date, time_from = performance
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': '\n'.join(content) or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        urls = page_urls(session)
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Les Musicales d Assy sitemap',
            event='crawler_fetch_failed',
            level='error',
            url=PAGES_SITEMAP_URL,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        raise

    records = []
    for url in sorted(urls):
        try:
            record = detail_record(session, url)
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Les Musicales d Assy page',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue
        if record:
            records.append(record)

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['url']
        ),
    )


class LesMusicalesDAssyFrCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lesmusicalesdassy_fr',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return get_concerts()


def main():
    LesMusicalesDAssyFrCrawler().run()


if __name__ == '__main__':
    main()
