import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = 'Du Bois Orchestra'
SOURCE_URL = 'https://www.duboisorchestra.org/'
SITEMAP_URL = urljoin(SOURCE_URL, 'pages-sitemap.xml')
REQUEST_TIMEOUT = 30
DATE_TIME_PATTERN = re.compile(
    r'(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\|\s*'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'(?P<date>[A-Z][a-z]+\s+\d{1,2},\s+\d{4})',
    re.IGNORECASE,
)


class DuBoisOrchestraCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='duboisorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[
            ('source_url', SOURCE_URL),
            ('source', SOURCE),
        ],
    )

    def _get_soup(self, url: str, *, xml: bool = False) -> BeautifulSoup:
        log_message('Fetching page', event='crawler_url_fetch', url=url)
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return BeautifulSoup(response.content, 'xml' if xml else 'html.parser')

    def _season_urls(self) -> list[str]:
        sitemap = self._get_soup(SITEMAP_URL, xml=True)
        urls = []
        for location in sitemap.find_all('loc'):
            url = location.get_text(strip=True)
            path = url.rstrip('/').rsplit('/', 1)[-1]
            if re.fullmatch(r'\d{4}(?:-\d{4})?-concert-season', path):
                urls.append(url)
        return sorted(set(urls))

    @staticmethod
    def _clean_text(node) -> str:
        return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).replace('\u200b', '').strip()

    def _parse_season_page(self, url: str) -> list[dict]:
        soup = self._get_soup(url)
        main = soup.find('main')
        if main is None:
            return []

        nodes = main.find_all(['h3', 'h4', 'p'], recursive=True)
        records = []
        title = None
        details: list[str] = []

        for index, node in enumerate(nodes):
            text = self._clean_text(node)
            if not text:
                continue
            if node.name == 'h3':
                title = text
                details = []
                continue

            match = DATE_TIME_PATTERN.fullmatch(text)
            if not match or title is None:
                details.append(text)
                continue

            date_value = datetime.strptime(match.group('date'), '%B %d, %Y').date().isoformat()
            time_value = datetime.strptime(
                re.sub(r'\s+', '', match.group('time')).upper(),
                '%I:%M%p' if ':' in match.group('time') else '%I%p',
            ).time().isoformat()

            venue_node = nodes[index + 1] if index + 1 < len(nodes) else None
            city_node = nodes[index + 2] if index + 2 < len(nodes) else None
            venue = self._clean_text(venue_node) if venue_node else ''
            city_text = self._clean_text(city_node) if city_node else ''
            city = city_text.split(',', 1)[0].strip() if ',' in city_text else ''
            if not venue or not city:
                log_message(
                    'Skipping event without a usable venue or city',
                    event='crawler_record_skipped',
                    url=url,
                    title=title,
                )
                title = None
                details = []
                continue

            records.append({
                'title': title,
                'date': date_value,
                'url': url,
                'time_from': time_value,
                'time_to': None,
                'venue': venue,
                'city': city,
                'description': '\n'.join(details) or None,
            })
            title = None
            details = []

        return records

    def scrape(self) -> list[dict]:
        records = []
        for url in self._season_urls():
            records.extend(self._parse_season_page(url))
        log_message(
            'Concert pages parsed',
            event='crawler_scrape_completed',
            record_count=len(records),
        )
        return records


def main():
    DuBoisOrchestraCrawler().run()


if __name__ == '__main__':
    main()
