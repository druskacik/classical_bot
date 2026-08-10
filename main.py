import schedule
import time
import importlib
import logging
import os
import sys
from pathlib import Path

from deployment.scraper_updater import ScraperUpdater, UpdaterConfig
from observability import configure_logging


RUN_JOBS_ON_STARTUP_ENV = "RUN_JOBS_ON_STARTUP"
logger = logging.getLogger(__name__)


def should_run_jobs_on_startup():
    """Return whether scheduled jobs should also run immediately on process startup."""
    return os.getenv(RUN_JOBS_ON_STARTUP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def discover_crawler_modules():
    """Discover and import all crawler main functions"""
    crawler_modules = []
    crawlers_dir = Path(__file__).parent / 'crawlers'
    
    for main_file in sorted(crawlers_dir.glob('*/*/main.py')):
        parts = main_file.relative_to(crawlers_dir).with_suffix('').parts
        module_path = 'crawlers.' + '.'.join(parts)
        try:
            module = importlib.import_module(module_path)
            if hasattr(module, 'main'):
                crawler_modules.append(module.main)
        except ImportError as e:
            logger.exception(
                "Failed to import crawler module",
                extra={"event": "crawler_import_failed", "crawler_module": module_path},
            )
    
    return crawler_modules

def run_job(main_function):
    try:
        main_function()
    except Exception as e:
        logger.exception(
            "Scheduled job failed",
            extra={"event": "job_failed", "job": main_function.__name__},
        )

def scheduler_main() -> None:
    configure_logging("classical-bot")
    # Discover all crawler modules
    crawler_functions = discover_crawler_modules()
    
    updater = ScraperUpdater(UpdaterConfig.from_environment())
    
    if should_run_jobs_on_startup():
        logger.info(
            "Running all jobs immediately on startup",
            extra={"event": "startup_jobs_enabled"},
        )
        updater.begin_daily_pipeline()
        try:
            for func in crawler_functions:
                run_job(func)
        finally:
            updater.finish_daily_pipeline()
    else:
        logger.info(
            "Skipping immediate job run",
            extra={"event": "startup_jobs_skipped"},
        )
    
    logger.info("Scheduling crawlers", extra={"event": "scheduler_started"})
    schedule.every().day.at("00:00").do(updater.begin_daily_pipeline)
    # Schedule all crawlers to run at 2-minute intervals starting at 00:01.
    for i, func in enumerate(crawler_functions):
        n = 1 + 2*i
        start_minute = n % 60
        start_hour = n // 60
        schedule_time = f"{start_hour:02d}:{start_minute:02d}"  # Format: 01:00, 01:02, etc.
        schedule.every().day.at(schedule_time).do(lambda f=func: run_job(f))

    final_job_number = 1 + 2 * len(crawler_functions)
    final_time = f"{final_job_number // 60:02d}:{final_job_number % 60:02d}"
    schedule.every().day.at(final_time).do(updater.finish_daily_pipeline)
    schedule.every(5).minutes.do(updater.request_update_check)
    
    while True:
        schedule.run_pending()
        time.sleep(1)


def main() -> None:
    if sys.argv[1:] == ["--scheduler-only"]:
        scheduler_main()
        return
    if sys.argv[1:]:
        raise SystemExit(f"Unknown arguments: {' '.join(sys.argv[1:])}")

    from automation.run_classical_bot_service import main as service_main

    service_main()


if __name__ == "__main__":
    main()
