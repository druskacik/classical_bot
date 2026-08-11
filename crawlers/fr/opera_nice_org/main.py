import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.opera-nice.org/'
AGENDA_URLS = (
    urljoin(SOURCE_URL, 'agenda/'),
    urljoin(SOURCE_URL, 'agenda/archives/'),
)
SOURCE = "Opéra Nice Côte d'Azur"
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.7',
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def canonical_url(value):
    parts = urlsplit(urljoin(SOURCE_URL, value or ''))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def make_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session


def fetch(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return response.text


def page_urls(session, start_url):
    html = fetch(session, start_url)
    soup = BeautifulSoup(html, 'html.parser')
    pages = {canonical_url(start_url): html}
    last_page = 1
    for link in soup.select('.event-pagination a[href]'):
        match = re.search(r'/page/(\d+)/', link.get('href', ''))
        if match:
            last_page = max(last_page, int(match.group(1)))
    for number in range(2, last_page + 1):
        url = urljoin(start_url, f'page/{number}/')
        pages[canonical_url(url)] = fetch(session, url)
    return pages


def parse_seed_cards(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    seeds = []
    for card in soup.select('#event-list article[itemtype="https://schema.org/Event"]'):
        title = clean_text(card.select_one('h2[itemprop="name"]'))
        venue = clean_text(card.select_one('[itemprop="location"] [itemprop="name"]'))
        locality = card.select_one('[itemprop="addressLocality"]')
        city = clean_text(locality.get('content')) if locality else ''
        city = re.sub(r'\s*,\s*France\s*$', '', city, flags=re.I).strip()
        link = card.select_one('a.link--read-more[href*="/agenda/"]')
        detail_url = canonical_url(link.get('href')) if link else ''
        schedule = card.select_one('[itemprop="eventSchedule"]')
        date_meta = schedule.select_one('meta[itemprop="startDate"]') if schedule else None
        time_meta = schedule.select_one('meta[itemprop="startTime"]') if schedule else None
        event_date = parse_iso_date(date_meta.get('content') if date_meta else None)
        time_from = parse_time(time_meta.get('content') if time_meta else None)
        if not detail_url and all((title, venue, city, event_date)):
            action = card.select_one('.event--action a[href]')
            record_url = urljoin(SOURCE_URL, action.get('href')) if action else f'{canonical_url(page_url)}#{card.get("id")}'
            seeds.append({
                'card_only': True, 'title': title, 'date': event_date, 'time_from': time_from,
                'venue': venue, 'city': city, 'url': record_url,
                'description': clean_text(card.select_one('.wrapper-info > p:not([itemprop="location"])')) or None,
            })
            continue
        if not all((title, detail_url)):
            log_message(
                'Skipped incomplete Opera Nice agenda item',
                event='crawler_item_skipped',
                level='warning',
                url=detail_url or page_url,
                error_type='IncompleteEventData',
                error_message='Required title or internal detail URL is missing',
            )
            continue
        root_url = re.sub(r'/\d{8}(?:-\d{4})?/?$', '/', detail_url)
        summary = clean_text(card.select_one('.wrapper-info > p:not([itemprop="location"])')) or None
        seeds.append({'card_only': False, 'title': title, 'venue': venue, 'city': city, 'url': root_url, 'summary': summary})
    return seeds


def parse_iso_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def parse_time(value):
    match = re.fullmatch(r'(\d{2}):(\d{2})(?::\d{2})?', value or '')
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{match.group(1)}:{match.group(2)}'


def detail_description(soup, summary):
    parts = []
    credits = [clean_text(item) for item in soup.select('.event--credits li')]
    if credits:
        parts.append('\n'.join(credits))
    if summary:
        parts.append(summary)
    for section in soup.select('#summary-target > section'):
        if section.get('id') in {'booking-dates', 'faq'}:
            continue
        content = clean_text(section.select_one('.entry-content'))
        if content and content not in parts:
            parts.append(content)
    return '\n\n'.join(parts) or None


def parse_detail(seed, html):
    soup = BeautifulSoup(html, 'html.parser')
    title = clean_text(soup.select_one('article.single--event h1')) or seed['title']
    venue = clean_text(soup.select_one('article.single--event .place-links [itemprop="name"]')) or seed['venue']
    # The Opera's first-party place filter contains Nice venues only. New listings
    # also expose "Nice, France" as addressLocality; archived cards omit it.
    city = seed['city'] or ('Nice' if venue else '')
    if not venue or not city:
        raise ValueError('Required venue or city is missing')
    description = detail_description(soup, seed['summary'])
    schedules = soup.select('#booking-dates [itemprop="eventSchedule"]')
    if not schedules:
        schedules = soup.select('article.single--event [itemprop="eventSchedule"]')[:1]
    records = []
    seen = set()
    for schedule in schedules:
        date_meta = schedule.select_one('meta[itemprop="startDate"]')
        time_meta = schedule.select_one('meta[itemprop="startTime"]')
        event_date = parse_iso_date(date_meta.get('content') if date_meta else None)
        time_from = parse_time(time_meta.get('content') if time_meta else None)
        key = (event_date, time_from)
        if not event_date or key in seen:
            continue
        seen.add(key)
        suffix = event_date.replace('-', '') + (f'-{time_from.replace(":", "")}' if time_from else '')
        occurrence_url = urljoin(seed['url'], f'{suffix}/')
        records.append({
            'title': title,
            'date': event_date,
            'url': occurrence_url,
            'time_from': time_from,
            'venue': venue,
            'city': city,
            'description': description,
        })
    return records


class OperaNiceOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='opera_nice_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='FR',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        session = make_session()
        seeds_by_url = {}
        records = []
        for start_url in AGENDA_URLS:
            for page_url, html in page_urls(session, start_url).items():
                for seed in parse_seed_cards(html, page_url):
                    if seed['card_only']:
                        records.append({key: value for key, value in seed.items() if key != 'card_only'})
                    else:
                        seeds_by_url.setdefault(seed['url'], seed)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(fetch, make_session(), url): (url, seed)
                for url, seed in seeds_by_url.items()
            }
            for future in as_completed(futures):
                url, seed = futures[future]
                try:
                    parsed = parse_detail(seed, future.result())
                    if not parsed:
                        raise ValueError('No valid event occurrence was found')
                    records.extend(parsed)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape Opera Nice event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    OperaNiceOrgCrawler().run()


if __name__ == '__main__':
    main()
