import os
import re
import time
import queue
import threading
import logging
import clipboard
import requests
import urllib.parse
import argparse
import signal
from typing import Optional, List, Tuple

from tqdm import tqdm
from extract_links import PatreonScraper

DL_FOLDER = "downloads"
POLL_TIME = 1.0
THREADS = 1
TIMEOUT = 60

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.patreon.com/",
}

file_pattern = re.compile(r"https://www\.patreon\.com/file\?h=\d+&m=\d+")
POST_PATTERN = re.compile(r"https://www\.patreon\.com/posts/[\w-]+")


class ProgressHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)


app_log = logging.getLogger("patreon-downloader")
app_log.setLevel(logging.INFO)

log_fmt = ProgressHandler()
log_fmt.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
app_log.addHandler(log_fmt)

work_q = queue.Queue()
extract_q = queue.Queue()
history: set[str] = set()
exit_flag = threading.Event()
active_count = 0
counter_lock = threading.Lock()

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_robust_session():
    session = requests.Session()
    session.headers.update(UA)
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


http = create_robust_session()


def is_file(text: str) -> bool:
    return bool(file_pattern.fullmatch(text))


def is_post(text: str) -> bool:
    return bool(POST_PATTERN.match(text))


def clean_path(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def get_name_from_headers(headers: dict) -> str:
    disp = headers.get("content-disposition", "")
    find = re.search(r"filename\*\=utf-8''([^;]+)", disp, re.I)
    if find:
        return urllib.parse.unquote(find.group(1))

    find = re.search(r'filename="([^"]+)"', disp, re.I)
    if find:
        return find.group(1)

    return "download.bin"


def start_download(url: str, creator: Optional[str] = None):
    global active_count
    with counter_lock:
        active_count += 1

    try:
        subfolder = clean_path(creator) if creator else "Misc"
        out_dir = os.path.join(DL_FOLDER, subfolder)
        os.makedirs(out_dir, exist_ok=True)

        with http.get(url, stream=True, timeout=TIMEOUT) as resp:
            resp.raise_for_status()

            fname = clean_path(get_name_from_headers(resp.headers))
            full_path = os.path.join(out_dir, fname)
            part_file = full_path + ".part"

            size = int(resp.headers.get("content-length", 0))

            if os.path.exists(full_path):
                app_log.info("[SKIP] Already exists: %s", fname)
                return

            offset = os.path.getsize(part_file) if os.path.exists(part_file) else 0

            req_headers = UA.copy()
            if offset > 0:
                if offset >= size:
                    os.replace(part_file, full_path)
                    app_log.info("[DONE] Completed from part: %s", fname)
                    return
                req_headers["Range"] = f"bytes={offset}-"
                app_log.info("[RESUME] %s from %d bytes", fname, offset)
            else:
                app_log.info("[START] %s (%d bytes)", fname, size)

            with http.get(
                url, stream=True, timeout=TIMEOUT, headers=req_headers
            ) as stream:
                write_mode = "ab" if stream.status_code == 206 else "wb"
                if write_mode == "wb":
                    offset = 0

                with open(part_file, write_mode) as f, tqdm(
                    total=size,
                    initial=offset,
                    unit="B",
                    unit_scale=True,
                    desc=f"[{creator[:10] if creator else '...'}] {fname[:20]}",
                    leave=False,
                ) as pbar:
                    for chunk in stream.iter_content(chunk_size=64 * 1024):
                        if exit_flag.is_set():
                            app_log.info("🛑 [STOP] Aborting %s", fname)
                            return
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

            os.replace(part_file, full_path)
            app_log.info("✅ [SAVED] %s -> %s", fname, subfolder)

    except Exception as err:
        if not exit_flag.is_set():
            app_log.error("Download error for %s: %s", url, err)
    finally:
        with counter_lock:
            active_count -= 1


def worker():
    while not exit_flag.is_set():
        try:
            job = work_q.get(timeout=1.0)
            if job is None:
                break
            link, author = job
            start_download(link, author)
            work_q.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            app_log.error("Worker error: %s", e)


def scraper_worker():
    app_log.debug("Scraper worker started.")
    try:
        with PatreonScraper() as scraper:
            while not exit_flag.is_set():
                try:
                    url = extract_q.get(timeout=1.0)
                    if url is None:
                        break

                    app_log.info("🔍 Processing post: %s", url)
                    links, creator = scraper.get_links_from_post(url)

                    count = 0
                    for l in links:
                        if l not in history:
                            history.add(l)
                            work_q.put((l, creator))
                            count += 1

                    if count > 0:
                        app_log.info(
                            "✨ [+] Added %d NEW links from %s",
                            count,
                            creator or "unknown",
                        )
                    elif links:
                        app_log.info("ℹ️ All links from this post already handled.")

                    extract_q.task_done()
                except queue.Empty:
                    continue
                except Exception as e:
                    app_log.error("Scraper worker error: %s", e)
    except Exception as e:
        app_log.error("Scraper fatal error: %s", e)
    app_log.debug("Scraper worker stopped.")


def handle_url(url: str, force: bool = False):
    url = url.strip()
    if not url:
        return

    if is_file(url):
        if force or url not in history:
            history.add(url)
            work_q.put((url, None))
            app_log.info("🎯 Added file to queue: %s", url)
    elif is_post(url):
        if force or url not in history:
            history.add(url)
            extract_q.put(url)
            app_log.info("📥 Post queued for extraction: %s", url)


def run():
    p = argparse.ArgumentParser(description="Patreon File Downloader")
    p.add_argument("urls", nargs="*", help="URLs to download")
    p.add_argument("--no-clipboard", action="store_true", help="No clipboard monitor")
    args = p.parse_args()

    def on_stop(sig, frame):
        app_log.info("\n[SHUTDOWN] Stopping...")
        exit_flag.set()

    signal.signal(signal.SIGINT, on_stop)

    workers = []
    for _ in range(THREADS):
        t = threading.Thread(target=worker)
        t.start()
        workers.append(t)

    scraper_t = threading.Thread(target=scraper_worker, daemon=True)
    scraper_t.start()

    try:
        import ctypes

        def get_seq():
            return ctypes.windll.user32.GetClipboardSequenceNumber()

    except Exception:

        def get_seq():
            return 0

    try:
        for u in args.urls:
            handle_url(u)

        if args.no_clipboard:
            app_log.info("🚀 CLI finished. Waiting for downloads...")
            while not exit_flag.is_set() and (
                not work_q.empty() or not extract_q.empty() or active_count > 0
            ):
                time.sleep(1.0)
        else:
            app_log.info("📋 Watching clipboard... (Ctrl+C to stop)")

            last_seq = get_seq()
            idle = True
            status_ts = 0

            while not exit_flag.is_set():
                curr_seq = get_seq()

                if curr_seq != last_seq:
                    last_seq = curr_seq
                    try:
                        curr = clipboard.paste()
                    except Exception:
                        curr = ""

                    if curr:
                        for p in curr.split():
                            if is_file(p) or is_post(p):
                                handle_url(p, force=True)

                q_val = work_q.qsize()
                ex_val = extract_q.qsize()
                busy = q_val > 0 or ex_val > 0 or active_count > 0

                if busy:
                    idle = False
                    if time.time() - status_ts > 10:
                        app_log.info(
                            "📊 Status: %d in queue, %d pulling, %d active",
                            q_val,
                            ex_val,
                            active_count,
                        )
                        status_ts = time.time()
                else:
                    if not idle:
                        app_log.info("✨ Finished batch! Ready for more... 📋")
                        idle = True
                        status_ts = 0

                time.sleep(POLL_TIME)

    except Exception as e:
        if not exit_flag.is_set():
            app_log.error("Main loop error: %s", e)
    finally:
        exit_flag.set()  # Ensure everyone stops
        extract_q.put(None)
        for _ in range(THREADS):
            work_q.put(None)

        for job in workers:
            job.join(timeout=2.0)

        scraper_t.join(timeout=2.0)

        if not exit_flag.is_set() and work_q.empty() and active_count == 0:
            print("\n" + "=" * 50)
            print("  🎉 DONE!")
            print("=" * 50 + "\n")
        else:
            app_log.info("👋 Bye.")


if __name__ == "__main__":
    run()
