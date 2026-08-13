import os
from dotenv import load_dotenv
import psycopg2
from .cities import clean_city_raw, resolve_city

load_dotenv()

INSERT_ADVISORY_LOCKS = {
    "classical_concert": "classical-bot-classical-concert-insert",
    "potential_event": "classical-bot-potential-event-insert",
}
CONCERT_INSERT_ADVISORY_LOCK = INSERT_ADVISORY_LOCKS["classical_concert"]

def upload_concerts(data: list[dict], table_name: str = 'classical_concert'):
    """
    Upload concerts to the database
    """
    DB_NAME = os.getenv('DB_NAME')
    DB_USER = os.getenv('DB_USER')
    DB_PASS = os.getenv('DB_PASS')
    DB_HOST = os.getenv('DB_HOST')
    DB_PORT = os.getenv('DB_PORT')
    
    conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
    cursor = conn.cursor()

    if table_name in INSERT_ADVISORY_LOCKS:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (INSERT_ADVISORY_LOCKS[table_name],),
        )
    
    new_concerts = []
    skipped_count = 0
    
    for concert in data:
        cursor.execute(
            f"SELECT id FROM {table_name} WHERE title = %s AND date = %s AND url = %s",
            (concert['title'], concert['date'], concert['url'])
        )
        exists = cursor.fetchone()
        
        if not exists:
            new_concerts.append(concert)
        else:
            skipped_count += 1
    
    inserted_count = 0
    if new_concerts:
        for concert in new_concerts:
            composers = concert.get('composers')
            is_concert_details_filled = True if composers is not None else False
            city_raw = clean_city_raw(concert.get('city'))
            city = resolve_city(cursor, city_raw, concert.get('source'))
            cursor.execute(
                f"INSERT INTO {table_name} (title, date, source, source_url, time_from, time_to, city_raw, country_code_raw, city_id, country_code_resolved, venue, url, type, description, composers, is_concert_details_filled) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    concert['title'], 
                    concert['date'], 
                    concert['source'],
                    concert['source_url'],
                    concert.get('time_from'),
                    concert.get('time_to'), 
                    city_raw,
                    concert.get('country_code'),
                    city.city_id if city else None,
                    city.country_code if city else None,
                    concert['venue'],
                    concert['url'], 
                    concert.get('type'),
                    concert.get('description'),
                    composers,
                    is_concert_details_filled
                )
            )
            inserted_count += 1
    
    conn.commit()
    cursor.close()
    conn.close()
    
    return inserted_count, skipped_count

def upload_potential_concerts(data: list[dict]):
    """
    Upload concerts to the database
    """
    return upload_concerts(data, table_name='potential_event')

class Concert:
    def __init__(self, title: str, date: str, source: str, time_from: str, time_to: str, city: str, venue: str, url: str, event_type: str, country_code: str | None = None):
        self.title = title
        self.date = date
        self.source = source
        self.time_from = time_from
        self.time_to = time_to
        self.city = city
        self.venue = venue
        self.url = url
        self.type = event_type
        self.country_code = country_code

    def __str__(self):
        return f"{self.title} - {self.date} - {self.city} - {self.venue} - {self.url}"

    def __repr__(self):
        return self.__str__()
    
    def json(self):
        return {
            'title': self.title,
            'date': self.date,
            'time_from': self.time_from,
            'time_to': self.time_to,
            'city': self.city,
            'country_code': self.country_code,
            'venue': self.venue,
            'url': self.url,
            'type': self.type,
        }
