from crawlers.base import CrawlerConfig
from crawlers.ticketportal import TicketportalCrawler, TicketportalSiteConfig


class TicketportalSkCrawler(TicketportalCrawler):
    config = CrawlerConfig(
        slug="ticketportal_sk",
        source="Ticketportal.sk",
        source_url="https://www.ticketportal.sk",
        country_code="SK",
        columns=["title", "date", "time_from", "venue", "city", "url", "organizer_url", "description"],
        upload_target="potential",
        front_fields=[
            ("source_url", "https://www.ticketportal.sk"),
            ("source", "Ticketportal.sk"),
        ],
    )
    site = TicketportalSiteConfig(
        base_url="https://www.ticketportal.sk",
        grid_url="https://tpskprodcdn.azureedge.net/Grid/Data?v=1&lang=SK",
        language="SK",
        category_names=frozenset({"Klasická hudba", "Opera", "Balet"}),
        excluded_organizer_urls=frozenset({
            "https://www.ticketportal.sk/NEvent/SLOVENSKA_FILHARMONIA",
        }),
    )


# Backwards-compatible name for imports outside the crawler worker.
TicketportalCrawler = TicketportalSkCrawler


def main():
    TicketportalSkCrawler().run()


if __name__ == "__main__":
    main()
