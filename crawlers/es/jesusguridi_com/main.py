import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://jesusguridi.com/'
CONCERT_URL = f'{SOURCE_URL}category/concierto/'
SOURCE = 'Jesús Guridi'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.7',
}

MONTHS = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
    'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9,
    'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12,
}

# The blog republishes notices from many Spanish cities.  A record is emitted
# only when its text names both one of these venues and its defensible city.
LOCATIONS = (
    (('teatro arriaga',), 'Teatro Arriaga', 'Bilbao'),
    (('teatro de la zarzuela',), 'Teatro de la Zarzuela', 'Madrid'),
    (('basílica de loiola', 'basilica de loiola'), 'Basílica de Loiola', 'Azpeitia'),
    (('basílica de maría magdalena', 'basilica de maria magdalena'),
     'Basílica de María Magdalena', 'Errenteria'),
    (('catedral de león', 'catedral de leon'), 'Catedral de León', 'León'),
    (('auditorio manuel de falla',), 'Auditorio Manuel de Falla', 'Granada'),
    (('plaza de la constitución',), 'Plaza de la Constitución', 'San Sebastián'),
    (('sagrado corazón de gabierrota', 'sagrado corazon de gabierrota'),
     'Centro Sagrado Corazón de Gabierrota', 'Errenteria'),
    (('calle toribio etxebarria',), 'Calle Toribio Etxebarria', 'Eibar'),
    (('sala principal de baluarte', 'baluarte'), 'Baluarte', 'Pamplona'),
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    text = str(value).replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def normalise(value):
    return value.lower().translate(str.maketrans('áéíóúüñ', 'aeiouun'))


def parse_dates(text):
    """Return explicit performance dates, excluding newspaper/post datelines."""
    matches = []
    patterns = (
        r'\b(?:funciones?\s+)?d[ií]as?\s+(\d{1,2})(?:\s+y\s+(\d{1,2}))?\s+de\s+'
        r'([a-záéíóú]+)\s+de\s+(20\d{2})',
        r'\b(?:se\s+(?:celebrar[aá]|estrenar[aá])\s+el|tendr[aá]\s+lugar\s+el)\s+'
        r'(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(20\d{2})',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = match.groups()
            if len(groups) == 4:
                days = [groups[0]] + ([groups[1]] if groups[1] else [])
                month_name, year = groups[2], groups[3]
            else:
                days, month_name, year = [groups[0]], groups[1], groups[2]
            month = MONTHS.get(month_name.lower())
            if not month:
                continue
            for day in days:
                try:
                    value = date(int(year), month, int(day)).isoformat()
                except ValueError:
                    continue
                if value not in matches:
                    matches.append(value)
    return matches


def parse_times(text):
    lead = text[:1200]
    values = re.findall(r'\b(?:a\s+las\s+)?([01]?\d|2[0-3])[.:]([0-5]\d)\s*h?\b', lead)
    return list(dict.fromkeys(f'{int(hour):02d}:{minute}' for hour, minute in values))


def parse_location(text):
    folded = normalise(text)
    for aliases, venue, city in LOCATIONS:
        if any(normalise(alias) in folded for alias in aliases):
            return venue, city
    return None


def parse_article(article):
    title_element = article.select_one('.entry-title')
    permalink = article.select_one('a[rel="bookmark"][href]')
    content = article.select_one('.entry-content')
    title = clean_text(title_element)
    description = clean_text(content)
    url = permalink.get('href', '').strip() if permalink else ''
    dates = parse_dates(description)
    location = parse_location(f'{title}\n{description}')
    if not title or not url or not description or not dates or not location:
        return []

    venue, city = location
    times = parse_times(description) or [None]
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': event_time,
            'venue': venue,
            'city': city,
            'country_code': 'ES',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date in dates
        for event_time in times
    ]


class JesusguridiComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jesusguridi_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='ES',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        seen_posts = set()
        try:
            for page_number in range(1, 101):
                url = CONCERT_URL if page_number == 1 else f'{CONCERT_URL}page/{page_number}/'
                response = session.get(url, timeout=45)
                if response.status_code == 404 and page_number > 1:
                    break
                response.raise_for_status()

                soup = BeautifulSoup(response.text, 'html.parser')
                articles = soup.select('article.category-concierto')
                new_articles = [article for article in articles if article.get('id') not in seen_posts]
                if not new_articles:
                    break
                for article in new_articles:
                    if article.get('id'):
                        seen_posts.add(article['id'])
                    records.extend(parse_article(article))
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Jesús Guridi concert category',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERT_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title']),
        )


def main():
    JesusguridiComCrawler().run()


if __name__ == '__main__':
    main()
