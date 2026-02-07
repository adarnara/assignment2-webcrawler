from threading import Thread

from inspect import getsource
from utils.download import download
from utils import get_logger
import scraper
import time


class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.worker_id = worker_id
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier
        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)

    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                time.sleep(0.1)
                continue

            # Enforce politeness BEFORE downloading (sleeps outside frontier lock
            # so other workers targeting different domains can proceed in parallel)
            self.frontier.wait_polite(tbd_url)

            self.logger.info(f"Worker-{self.worker_id} fetching: {tbd_url}")

            # Pre-check: HEAD request to skip oversized files
            if not prep_download(tbd_url, self.config, self.logger):
                self.frontier.mark_url_complete(tbd_url)
                continue

            resp = download(tbd_url, self.config, self.logger)

            # Skip empty / failed responses
            if not (resp and resp.raw_response):
                self.logger.info(f"Skipping {tbd_url}. No response or empty page.")
                self.frontier.mark_url_complete(tbd_url)
                continue

            # Skip oversized responses (by content-length header)
            try:
                cl = resp.raw_response.headers.get("content-length")
                if cl and float(cl) > 10 * 1048576:  # 10 MB
                    self.logger.info(f"Skipping {tbd_url}. File too large ({cl} bytes).")
                    self.frontier.mark_url_complete(tbd_url)
                    continue
            except (ValueError, AttributeError):
                pass

            self.logger.info(
                f"Downloaded {tbd_url}, status <{resp.status}>, "
                f"using cache {self.config.cache_server}.")

            scraped_urls = scraper.scraper(tbd_url, resp)
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)


def prep_download(url, config, logger=None):
    """
    Pre-check a URL by fetching headers to determine if the file size
    is within limits. Returns True if the URL should be downloaded.
    """
    import requests as req_lib
    try:
        host, port = config.cache_server
        resp = req_lib.head(
            f"http://{host}:{port}/",
            params=[("q", f"{url}"), ("u", f"{config.user_agent}")],
            timeout=10
        )
        content_length = resp.headers.get("content-length")
        if content_length:
            file_size_mb = float(content_length) / 1048576
            if file_size_mb > 10:  # 10 MB limit
                if logger:
                    logger.info(f"Skipping {url}. File size {file_size_mb:.2f}MB exceeds limit.")
                return False
        return True
    except Exception:
        # If pre-check fails, allow download attempt
        return True
