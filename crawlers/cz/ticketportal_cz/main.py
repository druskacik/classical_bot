from crawlers.base import CrawlerConfig
from crawlers.ticketportal import TicketportalCrawler, TicketportalSiteConfig


class TicketportalCzCrawler(TicketportalCrawler):
    config = CrawlerConfig(
        slug="ticketportal_cz",
        source="Ticketportal.cz",
        source_url="https://www.ticketportal.cz",
        country_code="CZ",
        columns=["title", "date", "time_from", "venue", "city", "url", "organizer_url", "description"],
        upload_target="potential",
        front_fields=[
            ("source_url", "https://www.ticketportal.cz"),
            ("source", "Ticketportal.cz"),
        ],
    )
    site = TicketportalSiteConfig(
        base_url="https://www.ticketportal.cz",
        grid_url="https://www.ticketportal.cz/Grid/Data?v=1&lang=CZ",
        language="CZ",
        category_names=frozenset({"Klasická", "Opera", "Balet", "Filmová hudba"}),
        detail_filter_category_names=frozenset({"Projekce"}),
    )


def main():
    TicketportalCzCrawler().run()


if __name__ == "__main__":
    main()
