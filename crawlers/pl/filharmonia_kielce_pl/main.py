import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://filharmonia.kielce.pl/'
PROGRAM_URL = urljoin(SOURCE_URL, 'repertuar.html')
SOURCE = 'Filharmonia Świętokrzyska im. Oskara Kolberga w Kielcach'
CITY = 'Kielce'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}

MONTHS = {
    'stycznia': 1,
    'lutego': 2,
    'marca': 3,
    'kwietnia': 4,
    'maja': 5,
    'czerwca': 6,
    'lipca': 7,
    'sierpnia': 8,
    'września': 9,
    'października': 10,
    'listopada': 11,
    'grudnia': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def get_page(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    declared = re.search(br'charset\s*=\s*["\']?([^"\'>; ]+)', response.content[:5000], re.I)
    response.encoding = declared.group(1).decode('ascii', 'ignore') if declared else response.apparent_encoding
    return response.text


def canonical_url(url):
    parts = urlsplit(url)
    return urlunsplit(('https', 'filharmonia.kielce.pl', parts.path, parts.query, parts.fragment))


def links_including_comments(html, base_url):
    """Return links from live markup and the site's commented season menus."""
    soup = BeautifulSoup(html, 'html.parser')
    fragments = [soup]
    fragments.extend(BeautifulSoup(str(node), 'html.parser') for node in soup.find_all(string=lambda x: isinstance(x, Comment)))
    links = set()
    for fragment in fragments:
        for tag in fragment.find_all('a', href=True):
            url = canonical_url(urljoin(base_url, tag['href']))
            if urlsplit(url).netloc == 'filharmonia.kielce.pl':
                links.add(url)
    return links


def discover_pages(session):
    home_html = get_page(session, SOURCE_URL)
    programme_html = get_page(session, PROGRAM_URL)
    links = links_including_comments(home_html, SOURCE_URL)
    links.update(links_including_comments(programme_html, PROGRAM_URL))

    programme_pages = {
        urlunsplit((*urlsplit(url)[:4], '')) for url in links
        if re.search(
            r'/(?:styczen|luty|marzec|kwiecien|maj|czerwiec|lipiec|sierpien|wrzesien|'
            r'pazdziernik|listopad|grudzien|lato|ffim)\d{2}\.html$',
            urlsplit(url).path,
        )
    }
    archive_seeds = {
        url for url in links
        if re.search(r'/arch(?:_\d{4})?/archiwum\d{2}_\d{2}_\d{2}[a-z]?\.html$', urlsplit(url).path)
        or re.search(r'/archiwum\d{2}_\d{2}_\d{2}[a-z]?\.html$', urlsplit(url).path)
    }

    archive_pages = set()
    for seed in sorted(archive_seeds):
        try:
            soup = BeautifulSoup(get_page(session, seed), 'html.parser')
        except requests.RequestException as error:
            log_message(
                'Failed to inspect archive index', event='crawler_page_failed', level='warning',
                url=seed, error_type=type(error).__name__, error_message=str(error),
            )
            continue
        for option in soup.select('select[name="urljump"] option[value]'):
            value = option.get('value', '')
            if value != 'none' and re.fullmatch(r'archiwum\d{2}_\d{2}_\d{2}[a-z]?\.html', value):
                archive_pages.add(canonical_url(urljoin(seed, value)))

    return sorted(programme_pages), sorted(archive_pages)


def parse_date(text, fallback_year=None):
    match = re.search(
        r'\b(\d{1,2})\s+(' + '|'.join(MONTHS) + r')(?:\s+(\d{4}))?',
        text.lower(),
    )
    if not match:
        return None
    year = int(match.group(3) or fallback_year or 0)
    try:
        return date(year, MONTHS[match.group(2)], int(match.group(1))).isoformat()
    except ValueError:
        return None


def parse_time(text):
    match = re.search(r'\bg(?:odz)?\.?\s*(\d{1,2})[.:](\d{2})\b', text.lower())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        return None
    return f'{int(match.group(1)):02d}:{match.group(2)}'


def parse_venue(header):
    text = clean_text(header).replace('\n', ' ')
    time_match = re.search(r'\bg(?:odz)?\.?\s*\d{1,2}[.:]\d{2}\s*,?', text.lower())
    venue = text[time_match.end():].strip(' ,.;-') if time_match else ''
    venue = re.sub(r'\s+', ' ', venue)
    return venue or None


def detail_url(container, page_url, anchor):
    for link in container.find_all('a', href=True):
        if '/press/' in link['href']:
            return canonical_url(urljoin(page_url, link['href']))
    return f'{page_url}#{anchor}' if anchor else page_url


def make_current_record(container, page_url, anchor):
    right = container.select_one('.right') or container
    header = right.find('h2')
    title_tag = right.find('h3')
    if not header or not title_tag:
        return None
    header_text = clean_text(header)
    year_match = re.search(r'(\d{2})\.html$', urlsplit(page_url).path)
    year = 2000 + int(year_match.group(1)) if year_match else None
    event_date = parse_date(header_text, year)
    venue = parse_venue(header_text)
    title = clean_text(title_tag).replace('\n', ' – ')
    description_parts = []
    for selector in ('.ensemble', '.members', '.description'):
        value = clean_text(right.select_one(selector))
        if value and value not in description_parts:
            description_parts.append(value)
    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': detail_url(right, page_url, anchor),
        'time_from': parse_time(header_text),
        'venue': venue,
        'city': CITY,
        'country_code': 'PL',
        'description': '\n\n'.join(description_parts) or None,
    }


def parse_programme_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    records = []
    for container in soup.select('div.container'):
        line = container.find_previous_sibling('div', class_='line')
        anchor_tag = line.find('a', attrs={'name': True}) if line else None
        record = make_current_record(container, page_url, anchor_tag.get('name') if anchor_tag else '')
        if record:
            records.append(record)
    return records


def parse_archive_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    press_link = soup.find('a', href=re.compile(r'(?:^|/)press/press_'))
    container = press_link.find_parent('p') if press_link else None
    if not container:
        container = next(
            (
                p for p in soup.find_all('p')
                if re.search(r'\b\d{1,2}\s+(?:' + '|'.join(MONTHS) + r')\b', clean_text(p).lower())
                and parse_time(clean_text(p))
            ),
            None,
        )
    if not container:
        return None
    lines = [line for line in clean_text(container).split('\n') if line]
    header_text = clean_text(press_link) if press_link else (lines[0] if lines else '')
    filename = urlsplit(page_url).path.rsplit('/', 1)[-1]
    file_date = re.search(r'archiwum(\d{2})_(\d{2})_(\d{2})', filename)
    fallback_year = 2000 + int(file_date.group(3)) if file_date else None
    event_date = parse_date(header_text, fallback_year)
    venue = parse_venue(header_text)
    bold_values = [clean_text(tag) for tag in container.find_all(['b', 'strong'])]
    bold_values = [value for value in bold_values if value and value != header_text]
    if not press_link:
        title = lines[1] if len(lines) > 1 else ''
    else:
        title = bold_values[0].replace('\n', ' – ') if bold_values else (lines[1] if len(lines) > 1 else '')
    full_text = clean_text(container)
    description = full_text
    if header_text and description.startswith(header_text):
        description = description[len(header_text):].strip()
    if not title or not event_date or not venue:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': canonical_url(urljoin(page_url, press_link['href'])) if press_link else page_url,
        'time_from': parse_time(header_text),
        'venue': venue,
        'city': CITY,
        'country_code': 'PL',
        'description': description or None,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    programme_pages, archive_pages = discover_pages(session)
    jobs = [(url, parse_programme_page) for url in programme_pages]
    jobs.extend((url, parse_archive_page) for url in archive_pages)
    records = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(get_page, session, url): (url, parser) for url, parser in jobs}
        for future in as_completed(futures):
            url, parser = futures[future]
            try:
                parsed = parser(future.result(), url)
                records.extend(parsed if isinstance(parsed, list) else ([parsed] if parsed else []))
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to scrape programme page', event='crawler_page_failed', level='warning',
                    url=url, error_type=type(error).__name__, error_message=str(error),
                )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title'], row['url']))


class FilharmoniaKielcePlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='filharmonia_kielce_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
    )

    def scrape(self):
        return get_concerts()


def main():
    FilharmoniaKielcePlCrawler().run()


if __name__ == '__main__':
    main()
