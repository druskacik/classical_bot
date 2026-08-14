import html
import re
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://oslokammermusikkfestival.no/'
SOURCE = 'Oslo Kammermusikkfestival'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        'Chrome/125.0 Safari/537.36'
    ),
    'Accept': 'application/json',
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
    'Referer': SOURCE_URL,
}
MONTHS = {
    'januar': 1, 'februar': 2, 'mars': 3, 'april': 4, 'mai': 5,
    'juni': 6, 'juli': 7, 'august': 8, 'september': 9,
    'oktober': 10, 'november': 11, 'desember': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'okt': 10, 'nov': 11, 'des': 12,
}


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_text(value, default_year=None):
    text = clean_text(value).casefold().replace('.', ' ')
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(20\d{2}))?\b',
        text,
    )
    if not match:
        return None
    year = int(match.group(3)) if match.group(3) else default_year
    if not year:
        return None
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'\b(?:kl\.?\s*)?(\d{1,2})[.:](\d{2})\b', clean_text(value), re.I)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return f'{hour:02d}:{minute:02d}' if hour < 24 and minute < 60 else None


def local_page_url(value):
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.netloc.casefold() not in {'oslokammermusikkfestival.no', 'www.oslokammermusikkfestival.no'}:
        return None
    if parsed.path.startswith('/wp-content/') or parsed.path == '/':
        return None
    return f'{SOURCE_URL.rstrip("/")}{parsed.path.rstrip("/")}/'


def record(title, event_date, url, time_from, venue):
    title = title.lstrip('*').strip()
    venue = venue.strip(' ,\n')
    if not title or not event_date or not url or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': 'Oslo',
        'description': None,
    }


def parse_tabbed_program(soup, year):
    records = []
    tabs = soup.select('[role="tab"]')
    panels = soup.select('[role="tabpanel"]')
    for tab, panel in zip(tabs, panels):
        event_date = parse_date_text(tab, year)
        if not event_date:
            continue
        for link in panel.select('h2 a[href]'):
            url = local_page_url(link.get('href'))
            card = link
            while card.parent and card.parent is not panel:
                card = card.parent
                if len(card.select('h2 a[href]')) == 1 and parse_time(card):
                    break
            text_widgets = card.select('.elementor-widget-text-editor')
            location_text = clean_text(text_widgets[-1]) if text_widgets else clean_text(card)
            venue = re.split(r'\bkl\.?\s*\d{1,2}[.:]\d{2}\b', location_text, 1, flags=re.I)[0]
            item = record(clean_text(link), event_date, url, parse_time(location_text), venue)
            if item:
                records.append(item)
    return records


def parse_legacy_program(soup, year):
    records = []
    for detail in soup.select('a.elementor-button[href]'):
        if clean_text(detail).casefold() != 'les mer':
            continue
        section = detail.find_parent('section')
        columns = section.select(':scope > .elementor-container > .elementor-column')
        date_heading = section.find_previous(['h2', 'h3', 'h4', 'h5']) if section else None
        event_date = parse_date_text(date_heading, year)
        if not event_date or len(columns) < 3:
            continue
        url = local_page_url(detail.get('href'))
        title_lines = clean_text(columns[1]).split('\n')
        title = title_lines[0]
        if title.endswith(':') and len(title_lines) > 1:
            title = f'{title} {title_lines[1]}'
        item = record(
            title,
            event_date,
            url,
            parse_time(columns[0]),
            clean_text(columns[2]),
        )
        if item:
            records.append(item)
    return records


def description_from_content(content):
    soup = BeautifulSoup(content, 'html.parser')
    parts = []
    for node in soup.select('.elementor-widget-text-editor'):
        text = clean_text(node)
        if text and text not in parts:
            parts.append(text)
    return '\n\n'.join(parts) or None


class OsloKammermusikkfestivalNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='oslokammermusikkfestival_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, params):
        response = session.get(API_URL, params=params, timeout=45)
        response.raise_for_status()
        return response

    def _program_pages(self, session):
        pages = []
        page_number = 1
        while True:
            response = self._get(session, {
                'search': 'Program',
                'per_page': 100,
                'page': page_number,
                '_fields': 'id,slug,link,title,content',
            })
            batch = response.json()
            for page in batch:
                match = re.fullmatch(r'program-(20\d{2})(?:-\d+)?', page.get('slug', ''))
                title = clean_text(page.get('title', {}).get('rendered'))
                if match and re.fullmatch(r'Program\s+20\d{2}', title, re.I):
                    pages.append((int(match.group(1)), page))
            total_pages = int(response.headers.get('X-WP-TotalPages', '1'))
            if page_number >= total_pages:
                break
            page_number += 1
        # Some years retain an obsolete empty page alongside the live programme.
        return sorted(pages, key=lambda value: (value[0], value[1]['id']))

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        program_pages = self._program_pages(session)
        records_by_year = {}
        for year, page in program_pages:
            soup = BeautifulSoup(page['content']['rendered'], 'html.parser')
            parsed = parse_tabbed_program(soup, year)
            if not parsed:
                parsed = parse_legacy_program(soup, year)
            parsed = [item for item in parsed if item['date'].startswith(f'{year}-')]
            if len(parsed) > len(records_by_year.get(year, [])):
                records_by_year[year] = parsed
        records = [item for year in sorted(records_by_year) for item in records_by_year[year]]

        descriptions = {}
        for item in records:
            url = item['url']
            if url not in descriptions:
                slug = urlparse(url).path.strip('/').split('/')[-1]
                try:
                    response = self._get(session, {
                        'slug': slug,
                        'per_page': 1,
                        '_fields': 'content',
                    })
                    pages = response.json()
                    descriptions[url] = (
                        description_from_content(pages[0]['content']['rendered']) if pages else None
                    )
                except (requests.RequestException, ValueError, KeyError) as error:
                    log_message(
                        'Failed to fetch Oslo Kammermusikkfestival event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    descriptions[url] = None
            item['description'] = descriptions[url]

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'] or '', item['title'], item['url']
        ))


def main():
    return OsloKammermusikkfestivalNoCrawler().run()


if __name__ == '__main__':
    main()
