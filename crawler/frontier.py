import os
import shelve
import heapq
import time
from threading import RLock
from urllib.parse import urlparse
from collections import deque

from utils import get_logger, get_urlhash, normalize
from scraper import is_valid


class Frontier(object):
    """
    Mercator-style frontier with front queue, per-domain back queues, and
    a min-heap for politeness scheduling.

    CRITICAL FIX vs. old version: get_tbd_url() NEVER sleeps while holding
    the lock.  If no back queue is ready yet it returns None immediately,
    letting the worker retry after a short sleep.  This allows all workers
    to proceed in parallel to different domains.
    """

    def __init__(self, config, restart):
        self.logger = get_logger("FRONTIER")
        self.config = config

        # ---- Mercator components ----
        # Front queue: URLs waiting to be assigned to a back queue
        self.front_queue = deque()

        # Back queues: one deque per active domain
        # Pool size = 3 * threads  (Mercator recommendation)
        self.num_back_queues = 3 * self.config.threads_count
        self.back_queues = [deque() for _ in range(self.num_back_queues)]

        # host -> back-queue id
        self.host_to_queue = {}

        # Min-heap of (ready_time, queue_id)
        self.ready_heap = []

        # Which back-queue slots are currently assigned to a host
        self.queue_in_use = [False] * self.num_back_queues

        # ---- Thread safety ----
        self.lock = RLock()

        # ---- Per-domain politeness tracking (used by wait_polite) ----
        self.last_request_time = {}

        # ---- Persistence ----
        if not os.path.exists(self.config.save_file) and not restart:
            self.logger.info(
                f"Did not find save file {self.config.save_file}, "
                f"starting from seed.")
        elif os.path.exists(self.config.save_file) and restart:
            self.logger.info(
                f"Found save file {self.config.save_file}, deleting it.")
            os.remove(self.config.save_file)

        self.save = shelve.open(self.config.save_file)

        if restart:
            for url in self.config.seed_urls:
                self.add_url(url)
        else:
            self._parse_save_file()
            if not self.save:
                for url in self.config.seed_urls:
                    self.add_url(url)

        self.logger.info(
            f"Mercator frontier initialised: {self.num_back_queues} back queues, "
            f"{len(self.front_queue)} URLs in front queue")

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _parse_save_file(self):
        """Load incomplete URLs from save file into front queue."""
        total_count = len(self.save)
        tbd_count = 0
        for url, completed in self.save.values():
            if not completed and is_valid(url):
                self.front_queue.append(url)
                tbd_count += 1
        self.logger.info(
            f"Found {tbd_count} urls to be downloaded from {total_count} "
            f"total urls discovered.")

    # ------------------------------------------------------------------
    # Internal Mercator helpers  (caller must hold self.lock)
    # ------------------------------------------------------------------

    def _extract_host(self, url):
        try:
            return urlparse(url).netloc.lower()
        except Exception:
            return None

    def _find_free_queue(self):
        """Return index of an unused back-queue slot, or None."""
        for i in range(self.num_back_queues):
            if not self.queue_in_use[i]:
                return i
        return None

    def _assign_to_back_queue(self, url):
        """
        Try to place *url* into the correct back queue.
        Returns True on success, False if no queue is available.
        """
        if not is_valid(url):
            return True          # drop silently, counts as "handled"

        host = self._extract_host(url)
        if not host:
            return True

        # Host already has a queue → just append
        if host in self.host_to_queue:
            qid = self.host_to_queue[host]
            self.back_queues[qid].append(url)
            return True

        # Need a new queue for this host
        qid = self._find_free_queue()
        if qid is None:
            return False         # all slots occupied

        self.host_to_queue[host] = qid
        self.queue_in_use[qid] = True
        self.back_queues[qid].append(url)
        # Ready immediately
        heapq.heappush(self.ready_heap, (time.time(), qid))
        self.logger.info(f"Assigned back queue {qid} to host {host}")
        return True

    def _drain_front_queue(self):
        """Move as many URLs as possible from front queue into back queues."""
        while self.front_queue:
            url = self.front_queue[0]
            if not is_valid(url):
                self.front_queue.popleft()
                continue
            if self._assign_to_back_queue(url):
                self.front_queue.popleft()
            else:
                break            # no free back-queue slot

    def _release_back_queue(self, queue_id):
        """
        Free a back-queue slot and try to reassign it to a waiting host
        from the front queue.
        """
        # Remove old host mapping
        old_host = None
        for host, qid in list(self.host_to_queue.items()):
            if qid == queue_id:
                old_host = host
                del self.host_to_queue[host]
                break

        if old_host:
            self.logger.info(f"Released back queue {queue_id} from host {old_host}")

        # Try to reassign to a new host from front queue
        while self.front_queue:
            url = self.front_queue.popleft()
            if not is_valid(url):
                continue
            host = self._extract_host(url)
            if not host:
                continue
            if host in self.host_to_queue:
                # Host already has a queue; put URL into it directly
                qid = self.host_to_queue[host]
                self.back_queues[qid].append(url)
                # Slot still free, keep looking for a *new* host
                continue
            # Found a new host → assign this slot
            self.host_to_queue[host] = queue_id
            self.queue_in_use[queue_id] = True
            self.back_queues[queue_id].append(url)
            heapq.heappush(self.ready_heap, (time.time(), queue_id))
            self.logger.info(f"Reassigned back queue {queue_id} to new host {host}")
            return

        # Nothing to reassign — mark slot as free
        self.queue_in_use[queue_id] = False
        self.logger.info(
            f"Back queue {queue_id} freed (no new host to assign); "
            f"front_queue size={len(self.front_queue)}")

    def _refill_queue_for_host(self, queue_id, host):
        """
        After a back queue is emptied, scan the front queue for more URLs
        belonging to *host* and refill.  Returns True if any were found.
        """
        found = []
        remaining = deque()
        while self.front_queue:
            url = self.front_queue.popleft()
            if self._extract_host(url) == host and is_valid(url):
                found.append(url)
            else:
                remaining.append(url)
        self.front_queue = remaining

        if found:
            self.back_queues[queue_id].extend(found)
            return True
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tbd_url(self):
        """
        Return the next URL to download, or None if nothing is ready.

        **Never sleeps while holding the lock.**  If the earliest back queue
        isn't ready yet (politeness timer), it returns None so the worker
        can sleep briefly on its own without blocking other workers.
        """
        with self.lock:
            # Push front-queue URLs into back queues
            self._drain_front_queue()

            if not self.ready_heap:
                return None

            # Peek at earliest ready time
            ready_time, queue_id = self.ready_heap[0]
            now = time.time()

            if ready_time > now:
                # Nothing ready yet — return None, worker will retry
                return None

            # Pop the entry
            heapq.heappop(self.ready_heap)

            # Edge case: back queue was emptied in the meantime
            if not self.back_queues[queue_id]:
                self._release_back_queue(queue_id)
                return None

            url = self.back_queues[queue_id].popleft()

            # Schedule next URL from this queue (politeness delay)
            if self.back_queues[queue_id]:
                next_ready = now + self.config.time_delay
                heapq.heappush(self.ready_heap, (next_ready, queue_id))
            else:
                # Queue drained — try to refill from front queue
                host = self._extract_host(url)
                if host and self._refill_queue_for_host(queue_id, host):
                    next_ready = now + self.config.time_delay
                    heapq.heappush(self.ready_heap, (next_ready, queue_id))
                else:
                    self._release_back_queue(queue_id)

            return url

    def add_url(self, url):
        """Add URL to frontier (front queue). Only valid, unseen URLs."""
        url = normalize(url)
        if not is_valid(url):
            return
        urlhash = get_urlhash(url)
        with self.lock:
            if urlhash not in self.save:
                self.save[urlhash] = (url, False)
                self.save.sync()
                self.front_queue.append(url)

    def mark_url_complete(self, url):
        """Mark URL as completed in persistent storage."""
        urlhash = get_urlhash(url)
        with self.lock:
            if urlhash not in self.save:
                self.logger.error(
                    f"Completed url {url}, but have not seen it before.")
            self.save[urlhash] = (url, True)
            self.save.sync()

    def wait_polite(self, url):
        """
        Enforce politeness: ensure at least time_delay seconds between
        requests to the same domain.

        Sleeps OUTSIDE the lock so other workers targeting different
        domains can proceed in parallel.
        """
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return

        while True:
            with self.lock:
                now = time.time()
                elapsed = now - self.last_request_time.get(domain, 0.0)
                if elapsed >= self.config.time_delay:
                    self.last_request_time[domain] = time.time()
                    return
                wait_needed = self.config.time_delay - elapsed

            # Sleep OUTSIDE the lock
            time.sleep(wait_needed)
