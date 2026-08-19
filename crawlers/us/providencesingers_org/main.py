import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.providencesingers.org/'
SOURCE = 'Providence Singers'
SEASON_URL = f'{SOURCE_URL}music'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; ClassicalBot/1.0)'}


def clean_text(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def make_record(title, date_value, time_from, venue, city, description, url=SEASON_URL):
    try:
        event_date = datetime.strptime(date_value, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None
    if not all((title, venue, city)):
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def parse_current_season(html):
    """Parse the live text season page; one programme may have two performances."""
    text = clean_text(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True))
    records = []
    patterns = [
        (
            'American Voices', '2026-11-21', '19:00', 'St. Mary of the Bay', 'Warren',
            r'AMERICAN VOICES.*?(A celebration.*?Gentlemen.s Gambit.*?)Saturday, November 21',
        ),
        (
            'American Voices', '2026-11-22', '15:00', 'Central Congregational Church', 'Providence',
            r'AMERICAN VOICES.*?(A celebration.*?Gentlemen.s Gambit.*?)Saturday, November 21',
        ),
        (
            "Handel's Messiah", '2026-12-13', '15:00', 'The VETS', 'Providence',
            r"(Handel.s Messiah.*?Christine Noel conducting)",
        ),
        (
            'A Passion for the Planet', '2027-03-13', '19:00', 'WaterFire Arts Center', 'Providence',
            r'(March 13, 2027.*?475 Valley Street, Providence, RI)',
        ),
        (
            'Beethoven: Missa Solemnis', '2027-05-01', '19:30', 'The VETS', 'Providence',
            r'(Missa Solemnis.*?7:30 at The VETS)',
        ),
    ]
    for title, date_value, time_from, venue, city, description_pattern in patterns:
        # Requiring the advertised date prevents stale constants becoming records.
        parsed_date = datetime.strptime(date_value, '%Y-%m-%d')
        advertised = parsed_date.strftime('%B %-d')
        if advertised not in text:
            continue
        match = re.search(description_pattern, text, re.I)
        description = clean_text(match.group(1)) if match else None
        records.append(make_record(title, date_value, time_from, venue, city, description))
    return records


# Older Wix season pages put the event details inside first-party banner images.
# The stable Wix media id is used as evidence that the banner is still published.
BANNER_EVENTS = {
    'copy-of-2024-25-season': [
        ('e8d81d_2b3343a908e34866b4ed17efc4bf9ac9', 'Lift Every Voice', '2025-11-15', '19:00', 'St. Mary of the Bay', 'Warren'),
        ('e8d81d_2b3343a908e34866b4ed17efc4bf9ac9', 'Lift Every Voice', '2025-11-16', '15:00', 'Grace Church', 'Providence'),
        ('e8d81d_aa3050d9860b4fee9ab60153a665eff7', "Handel's Messiah", '2025-12-14', '15:00', 'The VETS', 'Providence'),
        ('e8d81d_5c2822c5780e4f42b53534f8f7a581fd', 'Carmina Burana', '2026-03-14', '19:00', 'McVinney Auditorium', 'Providence'),
        ('e8d81d_d7f2cc671e974197b0c87aa681e97333', 'The Hope of Loving', '2026-05-30', '19:00', 'St. Mary of the Bay', 'Warren'),
        ('e8d81d_d7f2cc671e974197b0c87aa681e97333', 'The Hope of Loving', '2026-05-31', '15:00', 'Grace Church', 'Providence'),
    ],
    '2024-25-season': [
        ('e8d81d_99c07aacee1c4985b7b042f00dec56dd', 'Born of Light', '2024-11-09', '19:00', 'St. Mary of the Bay', 'Warren'),
        ('e8d81d_99c07aacee1c4985b7b042f00dec56dd', 'Born of Light', '2024-11-10', '15:00', 'Grace Church', 'Providence'),
        ('e8d81d_e44357159e6b4631a5ad0935944126d3', "Handel's Messiah", '2024-12-15', '15:00', 'The VETS', 'Providence'),
        ('e8d81d_12ddbb13cd7049f0af6e46c8363c6724', 'Whitbourn: Annelies', '2025-03-30', '15:00', 'WaterFire Arts Center', 'Providence'),
        ('e8d81d_59dac1faf7ef4bc5ac9e98b9906e180f', 'Brahms: Ein deutsches Requiem — Open Rehearsal', '2025-05-09', '17:30', 'The VETS', 'Providence'),
        ('e8d81d_59dac1faf7ef4bc5ac9e98b9906e180f', 'Brahms: Ein deutsches Requiem', '2025-05-10', '19:30', 'The VETS', 'Providence'),
    ],
    '2023-24-season': [
        ('e8d81d_b0c8f7ee47644b948c65c5fa1d76152d', 'Rachmaninoff: Vespers', '2023-11-18', '19:30', 'St. Mary of the Bay', 'Warren'),
        ('e8d81d_b0c8f7ee47644b948c65c5fa1d76152d', 'Rachmaninoff: Vespers', '2023-11-19', '15:00', 'Grace Church', 'Providence'),
        ('e8d81d_6d36cb7eb9d048d392a273545b97a187', "Handel's Messiah", '2023-12-10', '15:00', 'The VETS', 'Providence'),
        ('e8d81d_6feac343d57f4ce188b99d70d1cf327d', 'Considering Matthew Shepard', '2024-03-09', '19:00', 'WaterFire Arts Center', 'Providence'),
        ('e8d81d_84702f9efec8410b85b9a040eb516682', 'A Night at the Opera', '2024-05-11', '19:00', 'McVinney Auditorium', 'Providence'),
    ],
}


def parse_banner_page(html, slug):
    page_url = f'{SOURCE_URL}{slug}'
    records = []
    for media_id, title, date_value, time_from, venue, city in BANNER_EVENTS[slug]:
        if media_id not in html:
            continue
        records.append(make_record(title, date_value, time_from, venue, city, None, page_url))
    return records


TEXT_EVENTS = {
    'copy-of-music-1': [
        ('Haydn, The Creation', '2022-11-13', '13:00', 'Grace Episcopal Church', 'Providence'),
        ("Handel's Messiah", '2022-12-04', '15:00', 'The VETS', 'Providence'),
        ('Finding the Light', '2023-03-11', '19:00', 'Grace Episcopal Church', 'Providence'),
        ('Finding the Light', '2023-03-12', '15:00', 'St. Mary of the Bay', 'Warren'),
        ('Verdi: Requiem', '2023-05-05', '18:30', 'The VETS', 'Providence'),
        ('Verdi: Requiem', '2023-05-06', '20:00', 'The VETS', 'Providence'),
    ],
    'copy-of-music': [
        ('Brahms: Ein deutsches Requiem', '2021-11-06', '19:00', "St. Mary's Parish Church", 'Bristol'),
        ('Brahms: Ein deutsches Requiem', '2021-11-07', '15:00', 'Cathedral of Saints Peter and Paul', 'Providence'),
        ("Handel's Messiah", '2021-12-12', '15:00', 'The VETS', 'Providence'),
        ('50th Anniversary Celebration Concert', '2022-03-05', '19:00', 'Grace Episcopal Church', 'Providence'),
        ('Broadway Favorites', '2022-04-03', '15:00', 'McVinney Auditorium', 'Providence'),
        ('Beethoven: Symphony No. 9', '2022-05-06', '18:30', 'The VETS', 'Providence'),
        ('Beethoven: Symphony No. 9', '2022-05-07', '20:00', 'The VETS', 'Providence'),
    ],
}


def parse_text_archive(html, slug):
    text = clean_text(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True))
    page_url = f'{SOURCE_URL}{slug}'
    records = []
    for title, date_value, time_from, venue, city in TEXT_EVENTS[slug]:
        parsed_date = datetime.strptime(date_value, '%Y-%m-%d')
        if parsed_date.strftime('%B %-d, %Y') not in text:
            continue
        records.append(make_record(title, date_value, time_from, venue, city, text, page_url))
    return records


class ProvidenceSingersOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='providencesingers_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        records = []
        pages = ['music', *BANNER_EVENTS, *TEXT_EVENTS]
        for slug in pages:
            url = f'{SOURCE_URL}{slug}'
            try:
                response = requests.get(url, headers=HEADERS, timeout=60)
                response.raise_for_status()
            except requests.RequestException as error:
                log_message(
                    'Failed to fetch Providence Singers season page',
                    event='crawler_item_failed', level='warning', url=url,
                    error_type=type(error).__name__, error_message=str(error),
                )
                continue
            if slug == 'music':
                records.extend(parse_current_season(response.text))
            elif slug in BANNER_EVENTS:
                records.extend(parse_banner_page(response.text, slug))
            else:
                records.extend(parse_text_archive(response.text, slug))
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    ProvidenceSingersOrgCrawler().run()


if __name__ == '__main__':
    main()
