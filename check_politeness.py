"""
Politeness checker for the web crawler.

Reads Logs/Worker.log produced when you run:
    py -3.6 launch.py
(Workers write to Logs/Worker.log via get_logger(..., "Worker").)

Checks:
- Politeness delay: no two requests to the same domain within POLITENESS seconds
  (from config.ini). Checks both by completion time (Downloaded) and by request start
  time (fetching). Events are sorted by time per domain so interleaved logs are correct.
- All configured workers appear in the log (no workers hanging unused).
"""
import re
import os
from datetime import datetime
from collections import defaultdict
from urllib.parse import urlparse
from configparser import ConfigParser

# Log file written by launch.py workers
LOG_FILE = "Logs/Worker.log"
if not os.path.exists(LOG_FILE):
    print(f"ERROR: {LOG_FILE} not found. Run the crawler first: py -3.6 launch.py")
    exit(1)

with open(LOG_FILE, 'r') as f:
    lines = f.readlines()

# Also try "fetching" = request start time
fetch_events = []
download_events = []
for line in lines:
    if 'fetching:' in line:
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*fetching: (https?://[^\s]+)', line)
        if match:
            ts_str, url = match.groups()
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S,%f')
            host = urlparse(url).netloc.lower()
            fetch_events.append((ts, host))
    if 'Downloaded' in line and 'status' in line:
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Downloaded (https?://[^\s,]+)', line)
        if match:
            ts_str, url = match.groups()
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S,%f')
            host = urlparse(url).netloc.lower()
            download_events.append((ts, host))

def check_violations(events, label, min_delay_sec):
    """
    Check that for each domain, no two requests are closer than min_delay_sec.
    Events are sorted by time per domain so interleaved log order doesn't hide violations.
    """
    domain_times = defaultdict(list)
    for ts, host in events:
        domain_times[host].append(ts)
    violations = []
    for domain, times in sorted(domain_times.items()):
        times = sorted(times)  # chronological order so we check real gaps
        if len(times) >= 2:
            for i in range(1, len(times)):
                delta = (times[i] - times[i-1]).total_seconds()
                if delta < min_delay_sec:
                    violations.append((domain, delta, times[i-1], times[i]))
    print(f"{label}: {len(violations)} violations (min delay required: {min_delay_sec}s)")
    for domain, delta, t1, t2 in violations[:5]:
        print(f"  [X] {domain}: gap {delta:.3f}s (required >= {min_delay_sec}s)")
    return violations

# Read config (same as launch.py): worker count and politeness delay
expected_workers = 6
min_delay_sec = 0.5
if os.path.exists("config.ini"):
    try:
        c = ConfigParser()
        c.read("config.ini")
        expected_workers = int(c.get("LOCAL PROPERTIES", "THREADCOUNT"))
        min_delay_sec = float(c.get("CRAWLER", "POLITENESS"))
    except Exception:
        pass

# Count distinct workers that did at least one fetch (no hanging/unused workers)
worker_ids = set()
for line in lines:
    if "fetching:" in line or ("Downloaded" in line and "status" in line):
        m = re.search(r"Worker-(\d+)", line)
        if m:
            worker_ids.add(int(m.group(1)))
active_count = len(worker_ids)
missing = set(range(expected_workers)) - worker_ids

print("POLITENESS CHECK (logs from: py -3.6 launch.py)")
print("=" * 60)
print(f"Worker usage: {active_count}/{expected_workers} workers active in log.")
if missing:
    print(f"  [WARN] Workers never seen: {sorted(missing)} (may be OK if crawl was short).")
else:
    print("  [OK] All configured workers appear in the log.")
print(f"Politeness delay (from config.ini POLITENESS): {min_delay_sec}s")
print()
print("By COMPLETION time (Downloaded):")
v1 = check_violations(download_events, "Completions", min_delay_sec)
print("\nBy REQUEST START time (fetching):")
v2 = check_violations(fetch_events, "Starts", min_delay_sec)
if not v2:
    print("[OK] No violations when measuring request start times (delay kept properly).")
else:
    print("[X] Violations found: delay was not kept for the same domain.")
