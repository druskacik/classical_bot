import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.klavierfestival.de/'
SITEMAP_URL = f'{SOURCE_URL}sitemap.xml'
SOURCE = 'Klavier-Festival Ruhr'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.7',
}


def clean_text(value, separator=' '):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text(separator, strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text(separator, strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    if separator == '\n':
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    return re.sub(r'\s+', ' ', text).strip()


def get_response(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    return response


def concert_urls():
    soup = BeautifulSoup(get_response(SITEMAP_URL).content, 'xml')
    return sorted({
        clean_text(location)
        for location in soup.find_all('loc')
        if '/konzert/' in clean_text(location)
    })


def description_from_page(soup):
    parts = []
    subtitle = soup.select_one('.o-cover__summary__text__description')
    if clean_text(subtitle):
        parts.append(clean_text(subtitle))

    body = soup.select_one('.o-text--concert .o-text__content')
    body_text = clean_text(body, '\n')
    if body_text:
        parts.append(body_text)

    for details in soup.select('.o-accordions details'):
        heading = clean_text(details.select_one('.m-accordion__header__title'))
        if heading.casefold() != 'konzertprogramm':
            continue
        programme = clean_text(details.select_one('.m-accordion__content__inner'), '\n')
        if programme:
            parts.append(f'Konzertprogramm\n{programme}')
        break

    return '\n\n'.join(dict.fromkeys(parts)) or None


def city_from_address(address):
    lines = [clean_text(line) for line in address.stripped_strings]
    for line in reversed(lines):
        match = re.search(r'\b\d{5}\s+(.+)$', line)
        if match:
            return clean_text(match.group(1))
    return ''


def parse_concert(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    concert = soup.select_one('section.o-concert')
    if not concert:
        return None

    title = clean_text(concert.select_one('.o-cover__summary__text__title'))
    time_tag = concert.select_one('.o-concert__overview time[datetime]')
    address = concert.select_one('.o-concert__overview address')
    venue = clean_text(address.find('strong')) if address and address.find('strong') else ''
    city = city_from_address(address) if address else ''
    start_value = time_tag.get('datetime') if time_tag else ''

    if not title or not start_value or not venue or not city:
        return None
    try:
        start = datetime.fromisoformat(start_value)
    except ValueError:
        return None

    return {
        'title': title,
        'date': start.date().isoformat(),
        'url': url,
        'time_from': start.strftime('%H:%M') if 'T' in start_value else None,
        'venue': venue,
        'city': city,
        'country_code': 'DE',
        'description': description_from_page(concert),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def scrape_concert(url):
    return parse_concert(get_response(url).text, url)


class KlavierfestivalDeCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='klavierfestival_de',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='DE',
        upload_target='classical',
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
        dedupe_subset=['url'],
    )

    def scrape(self):
        urls = concert_urls()
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(scrape_concert, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    record = future.result()
                except requests.RequestException as error:
                    log_message(
                        'Failed to scrape concert detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
                else:
                    log_message(
                        'Skipped concert with incomplete required fields',
                        event='crawler_item_skipped',
                        level='warning',
                        url=url,
                    )

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    KlavierfestivalDeCrawler().run()


if __name__ == '__main__':
    main()
