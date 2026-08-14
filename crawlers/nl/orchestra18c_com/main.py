import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://orchestra18c.com/'
PROJECTS_API = f'{SOURCE_URL}wp-json/wp/v2/project'
SOURCE = 'Orkest van de Achttiende Eeuw'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.7',
}

MONTHS = {
    'januari': 1, 'februari': 2, 'maart': 3, 'april': 4,
    'mei': 5, 'juni': 6, 'juli': 7, 'augustus': 8,
    'september': 9, 'oktober': 10, 'november': 11, 'december': 12,
}

# The orchestra tours internationally. The site normally prints only city and
# hall, so these first-party city labels are also used to assign the event's ISO
# country code rather than incorrectly treating every tour date as Dutch.
CITY_COUNTRIES = {
    'Alkmaar': 'NL', 'Amsterdam': 'NL', 'Arnhem': 'NL', 'Breda': 'NL',
    'Den Bosch': 'NL', 'Den Haag': 'NL', 'Enschede': 'NL', 'Groningen': 'NL',
    'Haarlem': 'NL', 'Kropswolde': 'NL', 'Laren': 'NL', 'Leeuwarden': 'NL',
    'Leiden': 'NL', 'Middelburg': 'NL', 'Nijmegen': 'NL', 'Rotterdam': 'NL',
    'Tilburg': 'NL', 'Utrecht': 'NL', 'Vierhuizen': 'NL', 'Wirdum': 'NL',
    'Basel': 'CH', 'Bilbao': 'ES', 'Brugge': 'BE', 'Brussel': 'BE',
    'Eisenstadt': 'AT', 'Frankfurt': 'DE', 'Fukuoka': 'JP', 'Gent': 'BE',
    'Hamburg': 'DE', 'Heidelberg': 'DE', 'Herrenchiemsee': 'DE',
    'Keulen': 'DE', 'Kyoto': 'JP', 'Oldenburg': 'DE', 'Osaka': 'JP',
    'Paris': 'FR', 'Tokyo': 'JP', 'Zürich': 'CH',
}

BOILERPLATE_PREFIXES = (
    'Walenpleintje 157', 'Orkest van de Achttiende Eeuw Herengracht',
    'Het Orkest van de Achttiende Eeuw wordt genereus ondersteund',
    'Schrijf je in voor onze nieuwsbrief', '© ',
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fetch(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return response


def projects(session):
    """Read every published project, including projects in past seasons."""
    items = []
    page = 1
    while True:
        response = fetch(
            session,
            PROJECTS_API,
            params={'per_page': 100, 'page': page, 'orderby': 'id', 'order': 'asc'},
        )
        payload = response.json()
        items.extend(
            (item.get('link'), clean_text(item.get('title', {}).get('rendered')))
            for item in payload
            if item.get('link')
        )
        total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
        if page >= total_pages:
            break
        page += 1
    return list(dict.fromkeys(items))


def parse_datetime(value):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')\s+(\d{4})'
        r'(?:\s*,\s*(\d{1,2}):(\d{2}))?',
        clean_text(value).lower(),
    )
    if not match:
        return None, None
    day, month_name, year, hour, minute = match.groups()
    try:
        parsed = datetime(int(year), MONTHS[month_name], int(day))
    except ValueError:
        return None, None
    time_from = f'{int(hour):02d}:{minute}' if hour is not None else None
    # Midnight is used on several newer pages as an unset CMS default.
    if time_from == '00:00':
        time_from = None
    return parsed.date().isoformat(), time_from


def parse_location(value):
    text = clean_text(value).replace(' ,', ',')
    if not text:
        return None, None, None
    parts = [part.strip() for part in text.split(',', 1)]
    if len(parts) == 2:
        first, second = parts
        if first in CITY_COUNTRIES:
            city, venue = first, second
        elif second in CITY_COUNTRIES:
            venue, city = first, second
        else:
            return None, None, None
    else:
        # A few church dates are written as "Kerk Kropswolde" without a comma.
        match = re.match(r'(.+?)\s+(' + '|'.join(map(re.escape, CITY_COUNTRIES)) + r')$', text)
        if match:
            venue, city = match.groups()
        elif text == 'Herrenchiemsee':
            city, venue = 'Herrenchiemsee', 'Herrenchiemsee Festspiele'
        else:
            return None, None, None
    if not city or not venue or city == venue:
        return None, None, None
    return venue, city, CITY_COUNTRIES.get(city)


def detail_description(soup):
    parts = []
    for widget in soup.select('.elementor-location-single .elementor-widget-text-editor'):
        text = clean_text(widget)
        if not text or text.startswith(BOILERPLATE_PREFIXES):
            continue
        if text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


def scrape_project(session, url, title):
    soup = BeautifulSoup(fetch(session, url).text, 'html.parser')
    if not title:
        page_title = clean_text(soup.title)
        title = re.sub(r'\s+-\s+Orchestra18c\s*$', '', page_title)
    description = detail_description(soup)
    records = []
    for date_widget in soup.select('.elementor-widget-event-date'):
        date, time_from = parse_datetime(date_widget)
        container = date_widget.parent
        location_node = container.select_one('.elementor-widget-heading .elementor-heading-title')
        venue, city, country_code = parse_location(location_node)
        if not title or not date or not venue or not city or not country_code:
            continue
        records.append({
            'title': title,
            'date': date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'country_code': country_code,
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    records = []
    for url, title in projects(session):
        try:
            records.extend(scrape_project(session, url, title))
        except (requests.RequestException, ValueError) as error:
            log_message(
                'Failed to scrape project',
                event='crawler_item_failed',
                level='warning',
                url=url,
                error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'],
            record['city'], record['venue'],
        ),
    )


class Orchestra18cComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='orchestra18c_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NL',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        return get_concerts()


def main():
    Orchestra18cComCrawler().run()


if __name__ == '__main__':
    main()
