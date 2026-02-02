# Mercator Architecture — Visual Diagram

## High-level flow

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │                     FRONTIER                                 │
                    │                                                              │
  Workers           │   ┌──────────────┐      assign by host      ┌──────────────┐  │
  (scraped URLs) ───┼──►│ FRONT QUEUE  │─────────────────────────►│ BACK QUEUES   │  │
                    │   │ (all URLs)   │   host → queue_id       │ (per domain) │  │
                    │   └──────────────┘                         │ B = 3×threads│  │
                    │          ▲                                  └──────┬───────┘  │
                    │          │                                         │          │
                    │          │ refill (same host)                     │          │
                    │          └────────────────────────────────────────┘          │
                    │                                                              │
                    │   ┌──────────────────────────────────────────────────────┐   │
                    │   │ HEAP: (ready_time, queue_id)                         │   │
                    │   │ • Pop queue with earliest ready_time                │   │
                    │   │ • If ready_time > now → SLEEP (delay #1)             │   │
                    │   │ • After giving URL: push (give_out_time + 500ms, id) │   │
                    │   └──────────────────────────────────────────────────────┘   │
                    └─────────────────────────────────────────────────────────────┘
                                         │
                                         │ get_tbd_url() → URL
                                         ▼
                    ┌─────────────────────────────────────────────────────────────┐
                    │ WORKER                                                       │
                    │ • If last_download_time[domain] < 500ms ago → SLEEP (delay #2)│
                    │ • Download URL → record_download(url)                         │
                    │ • Scrape → frontier.add_url(...) → URLs go to FRONT QUEUE     │
                    └─────────────────────────────────────────────────────────────┘
```

## Where delay is handled

| Place | What | How |
|-------|------|-----|
| **Frontier (delay #1)** | Delay between *giving out* URLs from the *same back queue* (same domain) | Heap stores `(ready_time, queue_id)`. When giving a URL from queue *q*, next time *q* is ready = `actual_give_out_time + 500ms`. Before giving a URL, if `ready_time > now`, frontier **sleeps** until then. |
| **Worker (delay #2)** | Delay between *downloads* to the *same domain* (actual requests to server) | Shared `last_download_time[domain]`. Before downloading, if `now - last_download_time[domain] < 500ms`, worker **sleeps** the remainder. After download, worker calls `record_download(url)` to update the time. |

So: **front queue** holds all URLs; **assignment** maps each domain to one **back queue**; **delay** is enforced in the frontier (per back queue / per domain when giving URLs) and again in the worker (per domain when downloading).

## Detailed diagram (front queue → back queues → delay)

```
  ┌─────────────────────────────────────────────────────────────────────────────────┐
  │  FRONT QUEUE (FIFO)                                                               │
  │  [ url_A1, url_B1, url_A2, url_C1, url_A3, ... ]   ← add_url() from workers     │
  └────────────────────────────┬──────────────────────────────────────────────────────┘
                               │
                               │ _refill_back_queues() / _assign_url_to_back_queue()
                               │ • Take URL, get host (e.g. ics.uci.edu)
                               │ • If host already has a back queue → append to that queue
                               │ • If not and a back queue is free → assign it to host, push (now, queue_id) to heap
                               │ • If no free back queue → URL stays in front queue (logged)
                               ▼
  ┌─────────────────────────────────────────────────────────────────────────────────┐
  │  BACK QUEUES (fixed number B)          HEAP: min (ready_time, queue_id)           │
  │  ┌─────────┐ ┌─────────┐ ┌─────────┐   (t_ready_0, 0), (t_ready_1, 1), ...       │
  │  │ host A  │ │ host B  │ │ host C │   Pop minimum ready_time                   │
  │  │ [A1,A2] │ │ [B1]    │ │ [C1]   │   If ready_time > now → SLEEP (delay #1)     │
  │  └────┬────┘ └────┬────┘ └────┬────┘   Then give URL from that queue; push        │
  │       │           │           │        (give_out_time + 500ms, queue_id)         │
  └───────┼───────────┼───────────┼─────────────────────────────────────────────────┘
          │           │           │
          └───────────┴───────────┴──────────────► get_tbd_url() → one URL to one worker
                                                           │
                                                           ▼
  ┌─────────────────────────────────────────────────────────────────────────────────┐
  │  WORKER: before download, check last_download_time[domain]; if < 500ms → SLEEP (#2)│
  │  After download → record_download(url). Scrape → add_url() → back to FRONT QUEUE  │
  └─────────────────────────────────────────────────────────────────────────────────┘
```

Summary: **Front queue** collects URLs from workers; **back queues** are per-domain; **delay** is applied when the frontier selects the next URL (heap + sleep) and again in the worker before each download (shared last-download time + sleep).
