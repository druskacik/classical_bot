import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.operaen.no/'
SOURCE = 'Den Norske Opera & Ballett'
CALENDAR_API_URL = urljoin(SOURCE_URL, 'api/calendar/productions?language=no')
SITEMAP_URL = urljoin(SOURCE_URL, 'sitemap.xml')
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'nb-NO,nb;q=0.9,en;q=0.7',
}


def clean_text(value):
    if value is None:
        return ''
    text = str(value).replace('\xa0', ' ').replace('\u200b', '').replace('\u00ad', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def child_text(value):
    if isinstance(value, dict):
        return clean_text(value.get('name'))
    return clean_text(value)


def event_description(event):
    """Keep the synopsis and useful programme/credit evidence from JSON-LD."""
    parts = [clean_text(event.get('description'))]
    composer = event.get('composer')
    composers = composer if isinstance(composer, list) else [composer]
    names = [child_text(item) for item in composers if child_text(item)]
    if names:
        parts.append('Komponist: ' + ', '.join(dict.fromkeys(names)))

    for role in event.get('contributor') or []:
        if not isinstance(role, dict):
            continue
        role_name = clean_text(role.get('roleName'))
        contributor = child_text(role.get('contributor'))
        if role_name and contributor:
            parts.append(f'{role_name}: {contributor}')

    performers = event.get('performer') or []
    if not isinstance(performers, list):
        performers = [performers]
    names = [child_text(item) for item in performers if child_text(item)]
    if names:
        parts.append('Medvirkende: ' + ', '.join(dict.fromkeys(names)))
    return '\n\n'.join(dict.fromkeys(part for part in parts if part)) or None


def json_ld_events(html):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            document = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = document.get('@graph', []) if isinstance(document, dict) else document
        if not isinstance(nodes, list):
            nodes = [nodes]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get('@type') or []
            if isinstance(types, str):
                types = [types]
            if any(value == 'Event' or value.endswith('Event') for value in types):
                yield node


def production_urls_from_sitemap(xml):
    soup = BeautifulSoup(xml, 'xml')
    result = []
    for location in soup.find_all('loc'):
        url = clean_text(location.get_text())
        path_parts = [part for part in urlparse(url).path.split('/') if part]
        if len(path_parts) == 2 and path_parts[0] == 'forestillinger':
            result.append(url)
    return result


class OperaenNoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='operaen_no',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='NO',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from', 'venue'],
    )

    def _get(self, session, url):
        response = session.get(url, timeout=45)
        response.raise_for_status()
        return response

    def _production_urls(self, session):
        urls = []
        try:
            productions = self._get(session, CALENDAR_API_URL).json()
            urls.extend(urljoin(SOURCE_URL, item['url']) for item in productions if item.get('url'))
        except (requests.RequestException, ValueError, TypeError) as error:
            log_message(
                'Operaen calendar API could not be read',
                event='crawler_source_failed',
                level='warning',
                url=CALENDAR_API_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )

        try:
            sitemap = self._get(session, SITEMAP_URL).text
            urls.extend(production_urls_from_sitemap(sitemap))
        except requests.RequestException as error:
            log_message(
                'Operaen sitemap could not be read',
                event='crawler_source_failed',
                level='warning',
                url=SITEMAP_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        return list(dict.fromkeys(urls))

    def _record(self, event, page_url):
        title = clean_text(event.get('name'))
        try:
            start = datetime.fromisoformat(clean_text(event.get('startDate')).replace('Z', '+00:00'))
        except ValueError:
            return None

        location = event.get('location') or {}
        address = location.get('address') or {}
        venue = clean_text(location.get('name'))
        city = clean_text(address.get('addressLocality'))
        country = clean_text(address.get('addressCountry')).upper()
        if isinstance(address.get('addressCountry'), dict):
            country = clean_text(address['addressCountry'].get('name')).upper()
        if country in {'NORGE', 'NORWAY'}:
            country = 'NO'
        if not country:
            country = 'NO'

        if not all((title, venue, city)) or not re.fullmatch(r'[A-Z]{2}', country):
            return None
        return {
            'title': title,
            'date': start.date().isoformat(),
            'url': page_url,
            'time_from': start.strftime('%H:%M'),
            'venue': venue,
            'city': city,
            'country_code': country,
            'description': event_description(event),
        }

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        records = []
        for url in self._production_urls(session):
            try:
                html = self._get(session, url).text
            except requests.RequestException as error:
                log_message(
                    'Operaen production page could not be read',
                    event='crawler_item_failed',
                    level='warning',
                    url=url,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                continue
            for event in json_ld_events(html):
                record = self._record(event, url)
                if record:
                    records.append(record)

        return sorted(records, key=lambda item: (
            item['date'], item['time_from'], item['title'], item['venue']
        ))


def main():
    return OperaenNoCrawler().run()


if __name__ == '__main__':
    main()
