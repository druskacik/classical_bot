import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.musicalta.com/'
SOURCE = 'Musicalta'
API_URL = f'{SOURCE_URL}wp-json/wp/v2'
FESTIVAL_CATEGORY_SLUG = 'festival-ete'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}

MONTHS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}
MONTH_PATTERN = '|'.join(MONTHS)
DATE_RE = re.compile(
    rf'\b(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})\b', re.IGNORECASE
)
SHORT_DATE_RE = re.compile(rf'\b(\d{{1,2}})\s+({MONTH_PATTERN})\b', re.IGNORECASE)
TIME_RE = re.compile(r'\b([01]?\d|2[0-3])\s*[hH:]\s*([0-5]\d)?\b')
YEAR_RE = re.compile(r'\b(20\d{2})\b')

# The festival is explicitly presented as taking place in this compact Alsace
# territory. These names are only accepted when they occur in an event's own
# location block; they are not used as blind defaults.
CITIES = (
    'Rouffach', 'Eguisheim', 'Gueberschwihr', 'Pfaffenheim', 'Hattstatt',
    'Obermorschwihr', 'Westhalten', 'Gundolsheim', 'Soultzmatt', 'Osenbach',
    'Voegtlinshoffen', 'Herrlisheim-près-Colmar',
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u202f', ' ')
    return ' '.join(value.split())


def normalized(value):
    return clean_text(value).lower().translate(str.maketrans('àâäéèêëîïôöùûüç', 'aaaeeeeiioouuuc'))


def parse_date(title, text, category_names):
    searchable = f'{title}\n{text}'
    match = DATE_RE.search(normalized(searchable))
    if match:
        day, month, year = match.groups()
    else:
        short_match = SHORT_DATE_RE.search(normalized(title))
        years = YEAR_RE.findall(' '.join(category_names))
        if not short_match or not years:
            return None
        day, month = short_match.groups()
        year = max(years)
    try:
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    except ValueError:
        return None


def parse_time(title, text):
    match = TIME_RE.search(f'{title}\n{text}')
    if not match:
        return None
    return f'{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}'


def location_from_blurbs(soup):
    for blurb in soup.select('.et_pb_blurb'):
        heading = clean_text(blurb.select_one('.et_pb_module_header'))
        city = next((name for name in CITIES if normalized(heading) == normalized(name)), None)
        if not city:
            continue
        venue = clean_text(blurb.select_one('.et_pb_blurb_description'))
        if venue and normalized(venue) != normalized(city):
            return city, venue
    return None


def location_from_tokens(soup):
    tokens = [clean_text(value) for value in soup.stripped_strings]
    ignored = {
        'accueil', 'programme 2026', 'reservation', 'nous rejoindre', 'acces',
        'photos', 'archives', 'retour au programme', 'contactez-nous',
    }
    for index, token in enumerate(tokens):
        city = next((name for name in CITIES if normalized(token) == normalized(name)), None)
        if not city:
            continue
        for venue in tokens[index + 1:index + 5]:
            if normalized(venue) in ignored or normalized(venue) == normalized(city):
                continue
            if TIME_RE.fullmatch(venue) or DATE_RE.search(normalized(venue)):
                continue
            if 2 < len(venue) < 160:
                return city, venue
    return None


def parse_location(rendered_content):
    soup = BeautifulSoup(rendered_content or '', 'html.parser')
    location = location_from_blurbs(soup) or location_from_tokens(soup)
    if location:
        return location

    # A few older Divi records remain stored as shortcodes. Their location is
    # still structured as a blurb title followed by its paragraph content.
    for city in CITIES:
        match = re.search(
            rf'et_pb_blurb[^\]]*title\s*=\s*[»"“]?{re.escape(city)}\b[^\]]*\]'
            rf'.*?<p[^>]*>(.*?)</p>',
            rendered_content or '', re.IGNORECASE | re.DOTALL,
        )
        if match:
            venue = clean_text(BeautifulSoup(match.group(1), 'html.parser'))
            if venue and normalized(venue) != normalized(city):
                return city, venue
    return None


def description_from_content(rendered_content):
    soup = BeautifulSoup(rendered_content or '', 'html.parser')
    for unwanted in soup.select('script, style, nav, header, footer, form'):
        unwanted.decompose()
    lines = []
    for value in soup.stripped_strings:
        text = clean_text(value)
        if text and text not in lines:
            lines.append(text)
    description = '\n'.join(lines)
    return description or None


def parse_project(project, category_names):
    title_with_date = clean_text(project.get('title', {}).get('rendered'))
    # Preserve the site's capitalization and accents after stripping the prefix.
    title = re.sub(
        rf'^\s*\d{{1,2}}\s+(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)'
        rf'(?:\s+20\d{{2}})?\s*(?:\||[-–—])\s*',
        '', title_with_date, flags=re.IGNORECASE,
    ).strip()
    title = re.sub(
        r'^\s*(?:[01]?\d|2[0-3])\s*[hH:]\s*(?:[0-5]\d)?\s*[-–—]\s*',
        '', title,
    ).strip()
    rendered_content = project.get('content', {}).get('rendered') or ''
    body_text = clean_text(BeautifulSoup(rendered_content, 'html.parser'))
    event_date = parse_date(title_with_date, body_text, category_names)
    location = parse_location(rendered_content)
    url = clean_text(project.get('link'))
    if not title or not event_date or not location or not url:
        return None
    city, venue = location
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(title_with_date, body_text),
        'venue': venue,
        'city': city,
        'country_code': 'FR',
        'description': description_from_content(rendered_content),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MusicaltaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='musicalta_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        category_response = session.get(
            f'{API_URL}/project_category', params={'per_page': 100}, timeout=60
        )
        category_response.raise_for_status()
        categories = category_response.json()
        root = next((item for item in categories if item.get('slug') == FESTIVAL_CATEGORY_SLUG), None)
        if not root:
            raise RuntimeError('Musicalta festival category was not found')

        category_ids = {root['id']}
        changed = True
        while changed:
            changed = False
            for category in categories:
                if category.get('parent') in category_ids and category['id'] not in category_ids:
                    category_ids.add(category['id'])
                    changed = True
        names_by_id = {item['id']: clean_text(item.get('name')) for item in categories}

        records = []
        page = 1
        while True:
            response = session.get(
                f'{API_URL}/project',
                params={
                    'project_category': ','.join(str(value) for value in sorted(category_ids)),
                    'per_page': 100,
                    'page': page,
                    'orderby': 'id',
                    'order': 'asc',
                },
                timeout=60,
            )
            response.raise_for_status()
            projects = response.json()
            for project in projects:
                category_names = [
                    names_by_id[value]
                    for value in project.get('project_category', [])
                    if value in names_by_id
                ]
                record = parse_project(project, category_names)
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped incomplete Musicalta project',
                        event='crawler_item_skipped',
                        level='warning',
                        url=clean_text(project.get('link')) or SOURCE_URL,
                        error_type='IncompleteEventData',
                        error_message='Required title, date, URL, city, or venue is missing',
                    )

            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page >= total_pages:
                break
            if not projects:
                raise RuntimeError('Musicalta API advertised another page but returned no projects')
            page += 1

        return sorted(
            records,
            key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']),
        )


def main():
    MusicaltaComCrawler().run()


if __name__ == '__main__':
    main()
