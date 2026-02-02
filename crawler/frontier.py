import os
import shelve
import heapq
import time
from threading import Thread, RLock
from queue import Queue, Empty
from urllib.parse import urlparse
from collections import deque

from utils import get_logger, get_urlhash, normalize
from scraper import is_valid

class Frontier(object):
    def __init__(self, config, restart):
        self.logger = get_logger("FRONTIER")
        self.config = config
        
        # Mercator Architecture Components
        # Front queue: holds URLs waiting to be assigned to back queues
        self.front_queue = deque()
        
        # Back queues: one per active domain (fixed pool of B queues)
        # Mercator recommends B = 3 * number_of_threads
        self.num_back_queues = 3 * self.config.threads_count
        self.back_queues = [deque() for _ in range(self.num_back_queues)]
        
        # Host-to-back-queue mapping
        self.host_to_queue = {}  # Maps domain -> back_queue_id
        
        # Min-heap: tracks (ready_time, queue_id) for politeness
        self.ready_heap = []
        
        # Track which back queues are currently assigned
        self.queue_in_use = [False] * self.num_back_queues
        
        # Thread safety
        self.lock = RLock()
        
        # Track last DOWNLOAD completion time per domain (for true politeness at server level)
        self.last_download_time = {}
        
        # Persistence (shelve for tracking completed URLs)
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
        
        self.logger.info(f"Initialized Mercator frontier with {self.num_back_queues} back queues")

    def _parse_save_file(self):
        ''' Load incomplete URLs from save file into front queue '''
        total_count = len(self.save)
        tbd_count = 0
        for url, completed in self.save.values():
            if not completed and is_valid(url):
                self.front_queue.append(url)
                tbd_count += 1
        self.logger.info(
            f"Found {tbd_count} urls to be downloaded from {total_count} "
            f"total urls discovered.")

    def _extract_host(self, url):
        ''' Extract host/domain from URL '''
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except:
            return None

    def _find_available_back_queue(self):
        ''' Find an available (unused) back queue '''
        for i in range(self.num_back_queues):
            if not self.queue_in_use[i]:
                return i
        return None

    def _assign_url_to_back_queue(self, url):
        ''' 
        Assign a URL to appropriate back queue based on its host.
        Creates new back queue assignment if needed.
        '''
        host = self._extract_host(url)
        if not host:
            return False
        
        # Check if this host already has a back queue
        if host in self.host_to_queue:
            queue_id = self.host_to_queue[host]
            self.back_queues[queue_id].append(url)
            return True
        
        # Host doesn't have a queue yet, try to assign one
        queue_id = self._find_available_back_queue()
        if queue_id is not None:
            # Found an available queue
            self.host_to_queue[host] = queue_id
            self.queue_in_use[queue_id] = True
            self.back_queues[queue_id].append(url)
            
            # Add to heap with ready time = now (can crawl immediately)
            heapq.heappush(self.ready_heap, (time.time(), queue_id))
            self.logger.info(f"Assigned back queue {queue_id} to host {host}")
            return True
        
        # No available back queue: fixed pool of B queues is full (all assigned to other hosts).
        # URL stays in front queue until a back queue is released (e.g. when a host's queue
        # is drained and we release or reassign that queue).
        self.logger.info(
            f"No back queue available for host {host} (all {self.num_back_queues} in use); "
            f"URL remains in front queue (size={len(self.front_queue)})")
        return False

    def get_tbd_url(self):
        '''
        Get next URL to download, respecting politeness delays per domain.
        Implements Mercator architecture with front/back queues and heap.
        '''
        with self.lock:
            # Try to move URLs from front queue to back queues
            self._refill_back_queues()
            
            # If no back queues are ready, return None
            if not self.ready_heap:
                # Check if there are URLs in front queue that couldn't be assigned
                if self.front_queue:
                    # Try one more time to assign
                    if self._try_assign_from_front_queue():
                        pass  # Successfully assigned, continue below
                    else:
                        # All back queues occupied, no URLs ready yet
                        return None
                else:
                    # Truly empty frontier
                    return None
            
            # Get the earliest ready back queue
            ready_time, queue_id = heapq.heappop(self.ready_heap)
            
            # Wait if necessary (politeness delay)
            current_time = time.time()
            if ready_time > current_time:
                wait_time = ready_time - current_time
                self.logger.debug(f"Waiting {wait_time:.2f}s for politeness on queue {queue_id}")
                time.sleep(wait_time)
            
            # Record the ACTUAL time we're giving out the URL (after waiting)
            actual_give_out_time = time.time()
            
            # Get URL from this back queue
            if not self.back_queues[queue_id]:
                # Queue is empty (shouldn't happen, but handle it)
                self._release_back_queue(queue_id)
                return self.get_tbd_url()  # Recurse to try again
            
            url = self.back_queues[queue_id].popleft()
            
            # Check if back queue still has URLs
            if self.back_queues[queue_id]:
                # Queue still has URLs, push back to heap with new ready time
                # CRITICAL: Base next_ready_time on when we GAVE OUT the URL, not current processing time
                next_ready_time = actual_give_out_time + self.config.time_delay
                heapq.heappush(self.ready_heap, (next_ready_time, queue_id))
            else:
                # Queue is now empty, try to refill from front queue
                host = self._extract_host(url)
                refilled = self._refill_back_queue_for_host(queue_id, host, actual_give_out_time)
                
                if not refilled:
                    # No more URLs for this host, release the queue
                    self._release_back_queue(queue_id)
            
            return url

    def _refill_back_queues(self):
        ''' Try to move URLs from front queue to available back queues '''
        while self.front_queue:
            url = self.front_queue[0]  # Peek at front
            if self._assign_url_to_back_queue(url):
                self.front_queue.popleft()  # Successfully assigned, remove
            else:
                break  # Can't assign, stop trying

    def _try_assign_from_front_queue(self):
        ''' Try to assign one URL from front queue to a back queue '''
        if not self.front_queue:
            return False
        url = self.front_queue[0]
        if self._assign_url_to_back_queue(url):
            self.front_queue.popleft()
            return True
        return False

    def _refill_back_queue_for_host(self, queue_id, host, last_access_time):
        ''' 
        Try to refill a back queue with more URLs for the same host.
        Returns True if refilled, False if no more URLs for this host.
        last_access_time: The time when we last gave out a URL from this queue
        '''
        # Look through front queue for URLs from same host
        urls_for_host = []
        remaining_urls = deque()
        
        while self.front_queue:
            url = self.front_queue.popleft()
            if self._extract_host(url) == host:
                urls_for_host.append(url)
            else:
                remaining_urls.append(url)
        
        # Put non-matching URLs back
        self.front_queue = remaining_urls
        
        if urls_for_host:
            # Refill back queue with same host's URLs
            self.back_queues[queue_id].extend(urls_for_host)
            # Add back to heap with new ready time based on when we LAST accessed this domain
            next_ready_time = last_access_time + self.config.time_delay
            heapq.heappush(self.ready_heap, (next_ready_time, queue_id))
            return True
        
        return False

    def _release_back_queue(self, queue_id):
        ''' 
        Release a back queue (no more URLs for its host).
        Try to reassign it to a new host from front queue.
        '''
        # Remove old host mapping
        old_host = None
        for host, qid in list(self.host_to_queue.items()):
            if qid == queue_id:
                old_host = host
                del self.host_to_queue[host]
                break
        
        if old_host:
            self.logger.info(f"Released back queue {queue_id} from host {old_host}")
        
        # Try to assign this queue to a new host from front queue
        if self.front_queue:
            url = self.front_queue.popleft()
            host = self._extract_host(url)
            
            if host and host not in self.host_to_queue:
                # Assign queue to new host
                self.host_to_queue[host] = queue_id
                self.back_queues[queue_id].append(url)
                
                # Add to heap (ready immediately)
                heapq.heappush(self.ready_heap, (time.time(), queue_id))
                self.logger.info(f"Reassigned back queue {queue_id} to new host {host}")
            else:
                # No reassignment: URL's host already has a back queue (or invalid host).
                # Put URL back; this queue becomes available for the next new host.
                self.front_queue.appendleft(url)
                self.queue_in_use[queue_id] = False
                self.logger.info(
                    f"Released back queue {queue_id} not reassigned (front URL host already has queue or invalid); "
                    f"queue now available, front_queue size={len(self.front_queue)}")
        else:
            # No URLs in front queue to assign; queue is freed, no reassignment
            self.queue_in_use[queue_id] = False
            self.logger.info(
                f"Released back queue {queue_id} not reassigned (front queue empty); queue now available")

    def add_url(self, url):
        ''' Add URL to frontier (goes to front queue initially) '''
        url = normalize(url)
        urlhash = get_urlhash(url)
        
        with self.lock:
            if urlhash not in self.save:
                self.save[urlhash] = (url, False)
                self.save.sync()
                self.front_queue.append(url)
    
    def mark_url_complete(self, url):
        ''' Mark URL as completed in persistent storage '''
        urlhash = get_urlhash(url)
        
        with self.lock:
            if urlhash not in self.save:
                self.logger.error(
                    f"Completed url {url}, but have not seen it before.")
            else:
                self.save[urlhash] = (url, True)
                self.save.sync()
    
    def record_download(self, url):
        ''' Record that a download just completed for this URL's domain '''
        host = self._extract_host(url)
        if host:
            with self.lock:
                self.last_download_time[host] = time.time()
