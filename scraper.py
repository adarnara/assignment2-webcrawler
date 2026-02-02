import re
from urllib.parse import urlparse, urljoin, urldefrag
from lxml import html
from collections import defaultdict
import os
import json
import hashlib

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    # Implementation required.
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server. 200 is OK, you got the page. Other numbers mean that there was some kind of problem.
    # resp.error: when status is not 200, you can check the error here, if needed.
    # resp.raw_response: this is where the page actually is. More specifically, the raw_response has two parts:
    #         resp.raw_response.url: the url, again
    #         resp.raw_response.content: the content of the page!
    # Return a list with the hyperlinks (as strings) scrapped from resp.raw_response.content
    
    # Stop words from https://www.ranks.nl/stopwords (Assignment Q3 requirement)
    STOP_WORDS = {
        'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and',
        'any', 'are', "aren't", 'as', 'at', 'be', 'because', 'been', 'before', 'being',
        'below', 'between', 'both', 'but', 'by', "can't", 'cannot', 'could', "couldn't",
        'did', "didn't", 'do', 'does', "doesn't", 'doing', "don't", 'down', 'during',
        'each', 'few', 'for', 'from', 'further', 'had', "hadn't", 'has', "hasn't",
        'have', "haven't", 'having', 'he', "he'd", "he'll", "he's", 'her', 'here',
        "here's", 'hers', 'herself', 'him', 'himself', 'his', 'how', "how's", 'i',
        "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is', "isn't", 'it', "it's",
        'its', 'itself', "let's", 'me', 'more', 'most', "mustn't", 'my', 'myself',
        'no', 'nor', 'not', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'ought',
        'our', 'ours', 'ourselves', 'out', 'over', 'own', 'same', "shan't", 'she',
        "she'd", "she'll", "she's", 'should', "shouldn't", 'so', 'some', 'such',
        'than', 'that', "that's", 'the', 'their', 'theirs', 'them', 'themselves',
        'then', 'there', "there's", 'these', 'they', "they'd", "they'll", "they're",
        "they've", 'this', 'those', 'through', 'to', 'too', 'under', 'until', 'up',
        'very', 'was', "wasn't", 'we', "we'd", "we'll", "we're", "we've", 'were',
        "weren't", 'what', "what's", 'when', "when's", 'where', "where's", 'which',
        'while', 'who', "who's", 'whom', 'why', "why's", 'with', "won't", 'would',
        "wouldn't", 'you', "you'd", "you'll", "you're", "you've", 'your', 'yours',
        'yourself', 'yourselves'
    }
    
    def load_analytics():
        """Load analytics from file."""
        analytics = {
            'unique_pages': set(),
            'longest_page': ('', 0),
            'word_frequencies': defaultdict(int),
            'ics_subdomains': defaultdict(set),
            'content_hashes': set()
        }
        if os.path.exists('crawler_analytics.json'):
            try:
                with open('crawler_analytics.json', 'r') as f:
                    data = json.load(f)
                    analytics['unique_pages'] = set(data.get('unique_pages', []))
                    analytics['longest_page'] = tuple(data.get('longest_page', ['', 0]))
                    analytics['word_frequencies'] = defaultdict(int, data.get('word_frequencies', {}))
                    analytics['ics_subdomains'] = defaultdict(set)
                    for subdomain, pages in data.get('ics_subdomains', {}).items():
                        analytics['ics_subdomains'][subdomain] = set(pages)
                    analytics['content_hashes'] = set(data.get('content_hashes', []))
            except Exception:
                pass
        return analytics
    
    def save_analytics(analytics):
        """Save analytics to file."""
        try:
            data = {
                'unique_pages': list(analytics['unique_pages']),
                'longest_page': list(analytics['longest_page']),
                'word_frequencies': dict(analytics['word_frequencies']),
                'ics_subdomains': {k: list(v) for k, v in analytics['ics_subdomains'].items()},
                'content_hashes': list(analytics['content_hashes'])
            }
            with open('crawler_analytics.json', 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
    
    def process_chunks(lines_chunk):
        """Tokenization from Assignment 1."""
        punctuation = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'
        valid_token = re.compile(r'^[a-zA-Z0-9]+$')
        tokens = []
        for line in lines_chunk:
            words = line.lower().split()
            for word in words:
                stripped = word.strip(punctuation)
                if stripped and valid_token.match(stripped):
                    tokens.append(stripped)
        return tokens
    
    def get_text_content(tree):
        """Extract text content."""
        try:
            for element in tree.xpath('//script | //style | //meta | //link | //noscript'):
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
        except Exception:
            # If cleanup fails, still try to extract what we can.
            pass

        # Some pages contain invalid byte sequences that can trigger
        # UnicodeDecodeError inside lxml during XPath string extraction.
        try:
            return ' '.join(tree.xpath('//body//text()') or tree.xpath('//text()'))
        except UnicodeDecodeError:
            try:
                parts = []
                for t in tree.itertext():
                    if isinstance(t, bytes):
                        parts.append(t.decode('utf-8', errors='replace'))
                    else:
                        parts.append(str(t))
                return ' '.join(parts)
            except Exception:
                return ""
    
    def tokenize_text(text):
        """Tokenize text."""
        return process_chunks(text.split('\n'))
    
    def compute_hash(text):
        """Compute content hash."""
        normalized = ' '.join(text.lower().split())
        return hashlib.md5(normalized.encode()).hexdigest()
    
    def is_trap(url):
        """Detect crawler traps with robust pattern detection (COMBINED BEST FROM ALL VERSIONS)."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        netloc = parsed.netloc.lower()
        
        # 1. Very long URLs
        if len(url) > 200:
            return True
        
        # 2. Too many path segments (too deep)
        segments = [s for s in path.split('/') if s]
        if len(segments) > 8:
            return True
        
        # 3. Repeating path segments
        if len(segments) >= 3:
            segment_counts = defaultdict(int)
            for seg in segments:
                segment_counts[seg] += 1
            if any(count >= 3 for count in segment_counts.values()):
                return True
        
        # 4. Date patterns (COMPREHENSIVE - includes monthly archives from V1)
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',  # 2023-04-15 (daily)
            r'\d{4}/\d{2}/\d{2}',  # 2023/04/15 (daily)
            r'\d{8}',              # 20230415 (daily)
            r'/\d{4}/\d{2}(/|$)',  # /2019/10/ or /2019/10 (monthly archives) - FROM V1!
            r'day/\d',             # day/15
            r'month/\d',           # month/04
            r'year/\d',            # year/2023
        ]
        for pattern in date_patterns:
            if re.search(pattern, url):
                return True
        
        # 5. Calendar/date parameters (EXPANDED from V1 & V2)
        date_params = [
            'year=', 'month=', 'day=', 'date=', 
            'ical=', 'outlook-ical=',               # From V3
            'tribe-bar-date', 'eventdisplay',       # From V2
            'calendar',                             # From V2 (as query param)
        ]
        if any(param in query for param in date_params):
            return True
        
        # 6. Calendar keywords in path
        calendar_keywords = ['/calendar', '/events/', '/agenda', '\.ics']
        if any(kw in path for kw in ['/calendar', '/agenda', '.ics']):
            return True
        
        # Special case: /events/ with dates is a trap, but /event/ (singular) is usually OK
        if '/events/' in path and re.search(r'\d{4}', url):
            return True
        
        # 7. Pagination traps (with smart limits)
        pagination_patterns = [
            (r'page=(\d+)', 50),      # page=51+
            (r'offset=(\d+)', 500),   # offset=501+
            (r'start=(\d+)', 500),    # start=501+
            (r'/page/(\d+)', 50),     # /page/51+
        ]
        for pattern, max_val in pagination_patterns:
            match = re.search(pattern, url)
            if match and int(match.group(1)) > max_val:
                return True
        
        # 8. Session/tracking parameters
        trap_params = ['sid=', 'session=', 'phpsessid=', 'jsessionid=', 'token=', 'auth=']
        if any(param in query for param in trap_params):
            return True
        
        # 9. Action URLs (forms, authentication, etc.)
        action_patterns = ['action=', 'login', 'logout', 'signin', 'signout', 'register', 
                          'signup', 'subscribe', 'unsubscribe', 'replytocom=', 'download=']
        if any(pattern in url.lower() for pattern in action_patterns):
            return True
        
        # 10. Sorting/filtering parameters (creates duplicate content)
        filter_params = ['sort=', 'order=', 'filter=', 'view=', 'tab_']
        if sum(1 for param in filter_params if param in query) >= 2:
            return True
        
        # 10a. Apache directory listing sorting (uses semicolons)
        if re.search(r'\?C=[NMSD];O=[AD]', url):
            return True
        
        # 10b. Wiki/media browser - EXPANDED FROM V1 (more comprehensive)
        wiki_params = ['do=media', 'do=diff', 'do=revisions', 'do=index', 'do=login', 'rev=']
        if ('wiki.ics.uci.edu' in netloc or 'swiki.ics.uci.edu' in netloc) and 'doku.php' in path:
            # Block all wiki action/revision parameters that create infinite variations
            if any(param in query for param in wiki_params):
                return True
            # Block tab_ parameters (UI state variations)
            if 'tab_' in query:
                return True
        
        # 10c. Filter[] parameters (FROM V1) - block combinatorial filter explosions
        if 'filter[' in query or 'filter%5B' in query:
            return True
        
        # 11. Print/share/export versions (duplicate content)
        duplicate_params = ['print=', 'share=', 'export=', 'pdf=', 'format=']
        if any(param in query for param in duplicate_params):
            return True
        
        # 12. WordPress/CMS low-value paths (FROM V2)
        wordpress_paths = ['/category/', '/author/', '/tag/', '/feed/']
        if any(wp_path in path for wp_path in wordpress_paths):
            return True
        
        return False
    
    def is_low_info(text, word_count):
        """Detect low information pages."""
        if word_count < 50:
            return True
        words = tokenize_text(text)
        if len(words) > 100:
            unique_ratio = len(set(words)) / len(words)
            if unique_ratio < 0.1:
                return True
        return False
    
    def update_analytics(analytics, url, total_words, filtered_words):
        """Update analytics."""
        defragged_url, _ = urldefrag(url)
        analytics['unique_pages'].add(defragged_url)
        
        if total_words > analytics['longest_page'][1]:
            analytics['longest_page'] = (defragged_url, total_words)
        
        for word in filtered_words:
            analytics['word_frequencies'][word] += 1
        
        parsed = urlparse(defragged_url)
        hostname = parsed.netloc.lower()
        if hostname.endswith('.ics.uci.edu') or hostname == 'ics.uci.edu':
            analytics['ics_subdomains'][hostname].add(defragged_url)
        
        if len(analytics['unique_pages']) % 100 == 0:
            save_analytics(analytics)
    
    # Main logic
    links = []
    
    if resp.status != 200:
        if 600 <= resp.status <= 699:
            print(f"Cache error {resp.status} for {url}: {resp.error}")
        return links
    
    if resp.raw_response is None or resp.raw_response.content is None:
        return links
    
    try:
        content = resp.raw_response.content
        if len(content) == 0:
            return links
        
        content_type = resp.raw_response.headers.get('Content-Type', '')
        if content_type and 'text/html' not in content_type.lower():
            return links
        
        if len(content) > 10 * 1024 * 1024:
            return links
        
        # Decode bytes safely before feeding to lxml to avoid UnicodeDecodeError
        # during XPath text extraction on malformed encodings.
        decoded_html = None
        if isinstance(content, bytes):
            encoding = None
            try:
                encoding = getattr(resp.raw_response, 'encoding', None)
            except Exception:
                encoding = None
            if not encoding:
                try:
                    ct = resp.raw_response.headers.get('Content-Type', '') or ''
                    m = re.search(r'charset=([^\s;]+)', ct, flags=re.IGNORECASE)
                    if m:
                        encoding = m.group(1).strip('\'"')
                except Exception:
                    encoding = None
            if not encoding:
                encoding = 'utf-8'
            try:
                decoded_html = content.decode(encoding, errors='replace')
            except Exception:
                decoded_html = content.decode('utf-8', errors='replace')
        else:
            decoded_html = content

        tree = html.fromstring(decoded_html)
    except Exception:
        return links
    
    text = get_text_content(tree)
    all_words = tokenize_text(text)
    total_word_count = len(all_words)
    filtered_words = [w for w in all_words if w not in STOP_WORDS and len(w) >= 2]
    
    if is_low_info(text, total_word_count):
        return links
    
    analytics = load_analytics()
    content_hash = compute_hash(text)
    if content_hash in analytics['content_hashes']:
        return links
    analytics['content_hashes'].add(content_hash)
    
    update_analytics(analytics, url, total_word_count, filtered_words)
    save_analytics(analytics)
    
    base_url = resp.raw_response.url if resp.raw_response else url
    
    for anchor in tree.xpath('//a[@href]'):
        href = anchor.get('href', '').strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            continue
        try:
            absolute_url = urljoin(base_url, href)
            clean_url, _ = urldefrag(absolute_url)
            if clean_url.endswith('/'):
                clean_url = clean_url.rstrip('/')
            if not is_trap(clean_url):
                links.append(clean_url)
        except Exception:
            continue
    
    return links

def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    
    def is_trap(url):
        """Detect crawler traps with robust pattern detection (COMBINED BEST FROM ALL VERSIONS)."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        netloc = parsed.netloc.lower()
        
        # 1. Very long URLs
        if len(url) > 200:
            return True
        
        # 2. Too many path segments (too deep)
        segments = [s for s in path.split('/') if s]
        if len(segments) > 8:
            return True
        
        # 3. Repeating path segments
        if len(segments) >= 3:
            segment_counts = defaultdict(int)
            for seg in segments:
                segment_counts[seg] += 1
            if any(count >= 3 for count in segment_counts.values()):
                return True
        
        # 4. Date patterns (COMPREHENSIVE - includes monthly archives from V1)
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',  # 2023-04-15 (daily)
            r'\d{4}/\d{2}/\d{2}',  # 2023/04/15 (daily)
            r'\d{8}',              # 20230415 (daily)
            r'/\d{4}/\d{2}(/|$)',  # /2019/10/ or /2019/10 (monthly archives) - FROM V1!
            r'day/\d',             # day/15
            r'month/\d',           # month/04
            r'year/\d',            # year/2023
        ]
        for pattern in date_patterns:
            if re.search(pattern, url):
                return True
        
        # 5. Calendar/date parameters (EXPANDED from V1 & V2)
        date_params = [
            'year=', 'month=', 'day=', 'date=', 
            'ical=', 'outlook-ical=',               # From V3
            'tribe-bar-date', 'eventdisplay',       # From V2
            'calendar',                             # From V2 (as query param)
        ]
        if any(param in query for param in date_params):
            return True
        
        # 6. Calendar keywords in path
        calendar_keywords = ['/calendar', '/events/', '/agenda', '\.ics']
        if any(kw in path for kw in ['/calendar', '/agenda', '.ics']):
            return True
        
        # Special case: /events/ with dates is a trap, but /event/ (singular) is usually OK
        if '/events/' in path and re.search(r'\d{4}', url):
            return True
        
        # 7. Pagination traps (with smart limits)
        pagination_patterns = [
            (r'page=(\d+)', 50),      # page=51+
            (r'offset=(\d+)', 500),   # offset=501+
            (r'start=(\d+)', 500),    # start=501+
            (r'/page/(\d+)', 50),     # /page/51+
        ]
        for pattern, max_val in pagination_patterns:
            match = re.search(pattern, url)
            if match and int(match.group(1)) > max_val:
                return True
        
        # 8. Session/tracking parameters
        trap_params = ['sid=', 'session=', 'phpsessid=', 'jsessionid=', 'token=', 'auth=']
        if any(param in query for param in trap_params):
            return True
        
        # 9. Action URLs (forms, authentication, etc.)
        action_patterns = ['action=', 'login', 'logout', 'signin', 'signout', 'register', 
                          'signup', 'subscribe', 'unsubscribe', 'replytocom=', 'download=']
        if any(pattern in url.lower() for pattern in action_patterns):
            return True
        
        # 10. Sorting/filtering parameters (creates duplicate content)
        filter_params = ['sort=', 'order=', 'filter=', 'view=', 'tab_']
        if sum(1 for param in filter_params if param in query) >= 2:
            return True
        
        # 10a. Apache directory listing sorting (uses semicolons)
        if re.search(r'\?C=[NMSD];O=[AD]', url):
            return True
        
        # 10b. Wiki/media browser - EXPANDED FROM V1 (more comprehensive)
        wiki_params = ['do=media', 'do=diff', 'do=revisions', 'do=index', 'do=login', 'rev=']
        if ('wiki.ics.uci.edu' in netloc or 'swiki.ics.uci.edu' in netloc) and 'doku.php' in path:
            # Block all wiki action/revision parameters that create infinite variations
            if any(param in query for param in wiki_params):
                return True
            # Block tab_ parameters (UI state variations)
            if 'tab_' in query:
                return True
        
        # 10c. Filter[] parameters (FROM V1) - block combinatorial filter explosions
        if 'filter[' in query or 'filter%5B' in query:
            return True
        
        # 11. Print/share/export versions (duplicate content)
        duplicate_params = ['print=', 'share=', 'export=', 'pdf=', 'format=']
        if any(param in query for param in duplicate_params):
            return True
        
        # 12. WordPress/CMS low-value paths (FROM V2)
        wordpress_paths = ['/category/', '/author/', '/tag/', '/feed/']
        if any(wp_path in path for wp_path in wordpress_paths):
            return True
        
        return False
    
    try:
        parsed = urlparse(url)
        if parsed.scheme not in set(["http", "https"]):
            return False
        
        hostname = parsed.netloc.lower()
        path = parsed.path.lower()
        
        allowed = False
        if hostname == 'ics.uci.edu' or hostname.endswith('.ics.uci.edu'):
            allowed = True
        elif hostname == 'cs.uci.edu' or hostname.endswith('.cs.uci.edu'):
            allowed = True
        elif hostname == 'informatics.uci.edu' or hostname.endswith('.informatics.uci.edu'):
            allowed = True
        elif hostname == 'stat.uci.edu' or hostname.endswith('.stat.uci.edu'):
            allowed = True
        elif hostname == 'today.uci.edu' and path.startswith('/department/information_computer_sciences'):
            allowed = True
        
        if not allowed:
            return False
        
        if is_trap(url):
            return False
        
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            + r"|sql|db|sqlite|json|xml|rss|atom"
            + r"|apk|deb|rpm|img|war|ear"
            + r"|bak|tmp|log|cfg|conf|ini"
            + r"|ppsx|odt|ods|odp|odg|odf"
            + r"|svg|webp|woff|woff2|eot|ttf|otf)$", path)

    except TypeError:
        print ("TypeError for ", parsed)
        raise
