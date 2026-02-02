from threading import Thread
from urllib.parse import urlparse

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
        '''
        Worker thread loop with politeness enforced at download level.
        Uses shared frontier tracking to ensure 500ms between downloads per domain.
        '''
        while True:
            tbd_url = self.frontier.get_tbd_url()
            # if not tbd_url:
            #     self.logger.info("Frontier is empty. Stopping Crawler.")
            #     break

            if not tbd_url:
                time.sleep(0.1)
                continue
            
            # Extract domain for politeness checking
            try:
                domain = urlparse(tbd_url).netloc.lower()
            except:
                domain = None
            
            # Politeness: wait until 500ms since last download for this domain, then reserve
            # (reserve only inside lock so no race; sleep outside lock so others can get URLs)
            if domain:
                while True:
                    with self.frontier.lock:
                        now = time.time()
                        last = self.frontier.last_download_time.get(domain, 0.0)
                        time_since_last = (now - last) if last else 999.0
                        wait_time = max(0.0, self.config.time_delay - time_since_last)
                        if wait_time <= 0:
                            self.frontier.last_download_time[domain] = time.time()
                            break
                    if wait_time > 0:
                        self.logger.info(
                            f"Worker-{self.worker_id} blocked: politeness delay for {domain} "
                            f"(waiting {wait_time:.3f}s)")
                        time.sleep(wait_time)
            
            self.logger.info(f"Worker-{self.worker_id} fetching: {tbd_url}")
            resp = download(tbd_url, self.config, self.logger)
            
            # Record download completion time (overwrites reserve time; keeps semantics)
            self.frontier.record_download(tbd_url)
            
            self.logger.info(
                f"Downloaded {tbd_url}, status <{resp.status}>, "
                f"using cache {self.config.cache_server}.")
            
            scraped_urls = scraper.scraper(tbd_url, resp)
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            
            # NOTE: Politeness now enforced at DOWNLOAD level using shared tracking
