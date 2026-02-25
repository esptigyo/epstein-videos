#!/usr/bin/env python3
import argparse
import csv
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from requests.utils import requote_uri
from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

BASE = "https://www.justice.gov"
DEFAULT_DATASETS = [9, 10, 11, 12]
DEFAULT_EXTENSIONS = [
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".wmv",
    ".mpeg",
    ".mpg",
    ".mkv",
    ".webm",
    ".3gp",
    ".pdf",
]
DATASET_URL = "https://www.justice.gov/epstein/doj-disclosures/data-set-{dataset}-files"
AGE_BOOTSTRAP_URL = (
    "https://www.justice.gov/age-verify?destination="
    "/epstein/files/DataSet%2010/EFTA01683314.pdf"
)


class DriverSessionLost(RuntimeError):
    pass


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def make_driver(headless: bool) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def click_js(driver: webdriver.Chrome, elem) -> None:
    driver.execute_script("arguments[0].click();", elem)


def solve_interstitials(driver: webdriver.Chrome, rounds: int = 6) -> None:
    for _ in range(rounds):
        changed = False

        bot_buttons = driver.find_elements(
            By.XPATH, "//input[@type='button' and contains(@value,'not a robot')]"
        )
        if bot_buttons:
            click_js(driver, bot_buttons[0])
            time.sleep(3)
            changed = True

        yes_buttons = driver.find_elements(By.ID, "age-button-yes")
        if yes_buttons:
            click_js(driver, yes_buttons[0])
            time.sleep(2)
            changed = True

        if not changed:
            return


def ensure_authenticated(driver: webdriver.Chrome) -> None:
    driver.get(DATASET_URL.format(dataset=10))
    solve_interstitials(driver)

    driver.get(AGE_BOOTSTRAP_URL)
    solve_interstitials(driver)


def ensure_live_authenticated_session(
    driver: webdriver.Chrome, headless: bool, retries: int = 2
) -> Tuple[webdriver.Chrome, requests.Session]:
    last_exc: Optional[Exception] = None
    current = driver
    for _ in range(retries + 1):
        try:
            ensure_authenticated(current)
            return current, build_session_from_driver(current)
        except (InvalidSessionIdException, WebDriverException) as exc:
            msg = str(exc).lower()
            if isinstance(exc, InvalidSessionIdException) or "invalid session id" in msg:
                last_exc = exc
                try:
                    current.quit()
                except Exception:
                    pass
                current = make_driver(headless=headless)
                continue
            raise
    raise DriverSessionLost(f"Unable to restore browser session: {last_exc}")


def build_session_from_driver(driver: webdriver.Chrome) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        }
    )

    for c in driver.get_cookies():
        domain = c.get("domain") or ".justice.gov"
        path = c.get("path") or "/"
        session.cookies.set(c["name"], c["value"], domain=domain, path=path)

    # Force age cookie in case the UI flow did not set it in headless mode.
    session.cookies.set("justiceGovAgeVerified", "true", domain=".justice.gov", path="/")
    session.cookies.set("justiceGovAgeVerified", "true", domain="www.justice.gov", path="/")

    return session


def normalize_pdf_links(links: List[str], dataset: int) -> Set[str]:
    out: Set[str] = set()
    patt = re.compile(rf"/epstein/files/DataSet(?:%20|\s){dataset}/.+\.pdf$", re.IGNORECASE)

    for href in links:
        if not href:
            continue
        if href.startswith("/"):
            href = f"{BASE}{href}"
        if not href.lower().startswith("http"):
            continue
        if patt.search(href):
            out.add(requote_uri(href))
    return out


def load_page_hrefs(driver: webdriver.Chrome, url: str, retries: int = 1) -> Optional[List[str]]:
    attempts = 0
    while attempts <= retries:
        try:
            driver.get(url)
            solve_interstitials(driver)
            anchors = driver.find_elements(By.XPATH, "//a[@href]")
            return [a.get_attribute("href") for a in anchors if a.get_attribute("href")]
        except InvalidSessionIdException as exc:
            raise DriverSessionLost(str(exc)) from exc
        except (InvalidSessionIdException, WebDriverException) as exc:
            if "invalid session id" in str(exc).lower():
                raise DriverSessionLost(str(exc)) from exc
            attempts += 1
            log(f"Page load failed ({attempts}/{retries + 1}): {url} :: {exc.__class__.__name__}")
            time.sleep(2)
    return None


def extract_page_numbers(links: List[str], dataset: int) -> Set[int]:
    out: Set[int] = set()
    patt = re.compile(
        rf"^https?://www\.justice\.gov/epstein/doj-disclosures/data-set-{dataset}-files\?page=(\d+)$",
        re.IGNORECASE,
    )
    for href in links:
        if not href:
            continue
        m = patt.match(href)
        if m:
            out.add(int(m.group(1)))
    return out


def collect_dataset_pdf_urls(
    driver: webdriver.Chrome,
    dataset: int,
    max_pages: int,
    refresh_before_each_page: bool,
    page_delay: float,
) -> Set[str]:
    start_url = DATASET_URL.format(dataset=dataset)
    all_pdfs: Set[str] = set()
    denied_pages = 0

    log(f"Open dataset page: {start_url}")
    if refresh_before_each_page:
        try:
            ensure_authenticated(driver)
        except (InvalidSessionIdException, WebDriverException) as exc:
            if isinstance(exc, InvalidSessionIdException) or "invalid session id" in str(exc).lower():
                raise DriverSessionLost(str(exc)) from exc
            raise
    hrefs = load_page_hrefs(driver, start_url, retries=1)
    if hrefs is None:
        log(f"Dataset {dataset}: unable to load first page, skipping dataset.")
        return all_pdfs

    all_pdfs |= normalize_pdf_links(hrefs, dataset)

    page_numbers = extract_page_numbers(hrefs, dataset)
    max_page_from_pager = max(page_numbers) if page_numbers else 0
    last_page = max_page_from_pager
    if max_pages > 0:
        last_page = min(last_page, max_pages - 1)

    page_count = 1
    for page in range(1, last_page + 1):
        page_url = f"{start_url}?page={page}"
        log(f"Open dataset page: {page_url}")
        if page_delay > 0:
            time.sleep(page_delay)
        if refresh_before_each_page:
            try:
                ensure_authenticated(driver)
            except (InvalidSessionIdException, WebDriverException) as exc:
                if isinstance(exc, InvalidSessionIdException) or "invalid session id" in str(exc).lower():
                    raise DriverSessionLost(str(exc)) from exc
                raise

        hrefs = load_page_hrefs(driver, page_url, retries=1)
        if hrefs is None:
            log(f"Dataset {dataset} page {page}: browser session failed; stopping this dataset.")
            break
        pdfs = normalize_pdf_links(hrefs, dataset)
        page_count += 1

        if not pdfs and "access denied" in driver.page_source.lower():
            denied_pages += 1
            log(f"Dataset {dataset} page {page}: access denied")
            if denied_pages >= 3:
                log("Stopping early due to repeated denied pages.")
                break
            continue

        denied_pages = 0
        all_pdfs |= pdfs

    log(f"Dataset {dataset}: discovered {len(all_pdfs)} PDF links across {page_count} page(s)")
    return all_pdfs


def pdf_contains_marker(session: requests.Session, pdf_url: str, marker: str) -> str:
    url = requote_uri(pdf_url)
    try:
        resp = session.get(url, timeout=120, allow_redirects=True)
    except requests.RequestException:
        return "error"

    final_url = resp.url or url
    if "/age-verify" in final_url:
        return "refresh"
    if resp.status_code != 200:
        return "miss"

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "pdf" not in ctype and not resp.content.startswith(b"%PDF"):
        return "miss"

    marker_b = marker.encode("utf-8").lower()
    if marker_b in resp.content.lower():
        return "hit"
    return "miss"


def stem_from_pdf_url(pdf_url: str) -> Optional[str]:
    p = urlparse(pdf_url)
    if not p.path.lower().endswith(".pdf"):
        return None
    stem_path = p.path[:-4]
    return f"{p.scheme}://{p.netloc}{stem_path}"


def video_signature_kind(first: bytes) -> Optional[str]:
    if len(first) >= 12 and first[4:8] == b"ftyp":
        return "mp4"
    if len(first) >= 12 and first[:4] == b"RIFF" and first[8:11] == b"AVI":
        return "avi"
    if len(first) >= 4 and first[:4] == b"\x1aE\xdf\xa3":
        return "mkv"
    if len(first) >= 4 and first[:4] == b"OggS":
        return "ogv"
    return None


def looks_like_html_or_pdf(content_type: str, first: bytes) -> bool:
    c = (content_type or "").lower()
    if "text/html" in c or "application/pdf" in c:
        return True
    head = first[:16].lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return True
    if first.startswith(b"%PDF"):
        return True
    return False


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def unique_dest(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.stem
    ext = path.suffix
    i = 2
    while True:
        cand = path.with_name(f"{base}_{i}{ext}")
        if not cand.exists():
            return cand
        i += 1


def download_candidate(
    session: requests.Session,
    candidate_url: str,
    output_dir: Path,
    force: bool,
    probe_only: bool,
) -> Optional[Dict[str, str]]:
    url = requote_uri(candidate_url)

    try:
        with session.get(url, stream=True, timeout=180, allow_redirects=True) as resp:
            final_url = resp.url or url
            content_type = resp.headers.get("Content-Type", "")

            if "/age-verify" in final_url:
                return {"status": "refresh"}
            if resp.status_code != 200:
                return None

            first = b""
            for chunk in resp.iter_content(chunk_size=1024 * 64):
                if chunk:
                    first = chunk
                    break

            if not first:
                return None

            sig = video_signature_kind(first)
            ctype_is_video = content_type.lower().startswith("video/")
            if looks_like_html_or_pdf(content_type, first):
                return None
            if not sig and not ctype_is_video:
                return None

            ext_from_sig = sig or Path(urlparse(final_url).path).suffix.lower().lstrip(".")
            if not ext_from_sig:
                ext_from_sig = "mp4"

            stem_name = Path(urlparse(final_url).path).stem
            fname = safe_filename(f"{stem_name}.{ext_from_sig}")
            dest = unique_dest(output_dir / fname)

            if probe_only:
                return {
                    "status": "ok",
                    "file": dest.name,
                    "bytes": str(resp.headers.get("Content-Length", "")),
                    "url": final_url,
                }

            if dest.exists() and not force:
                return {
                    "status": "ok",
                    "file": dest.name,
                    "bytes": str(dest.stat().st_size),
                    "url": final_url,
                }

            tmp = dest.with_suffix(dest.suffix + ".download")
            with open(tmp, "wb") as f:
                f.write(first)
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

            tmp.replace(dest)
            return {
                "status": "ok",
                "file": dest.name,
                "bytes": str(dest.stat().st_size),
                "url": final_url,
            }

    except requests.RequestException:
        return None


def write_manifest(path: Path, rows: List[Dict[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "bytes", "url"])
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "file": r.get("file", ""),
                    "bytes": r.get("bytes", ""),
                    "url": r.get("url", ""),
                }
            )


def zip_output(folder: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in folder.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.name)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Selenium-based DOJ Epstein video downloader filtered by marker text "
            "inside PDF placeholders"
        )
    )
    p.add_argument("--output-dir", default="epstein-videos-sel")
    p.add_argument("--zip-path", default="epstein-videos-sel.zip")
    p.add_argument("--datasets", nargs="+", type=int, default=DEFAULT_DATASETS)
    p.add_argument("--pdf-marker", default="No Images Produced")
    p.add_argument("--extensions", nargs="+", default=DEFAULT_EXTENSIONS)
    p.add_argument("--headful", action="store_true", help="Run Chrome non-headless")
    p.add_argument(
        "--refresh-before-each-page",
        action="store_true",
        help="Re-authenticate before each dataset page request (slower, but can reduce blocking)",
    )
    p.add_argument(
        "--page-delay",
        type=float,
        default=0.75,
        help="Delay in seconds before each dataset page request",
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing files")
    p.add_argument("--probe-only", action="store_true", help="Find video URLs without downloading")
    p.add_argument("--max-stems", type=int, default=0, help="Limit stems for testing")
    p.add_argument(
        "--refresh-every",
        type=int,
        default=25,
        help="Refresh browser/session cookies every N stems",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Limit dataset pagination pages to crawl (0 = all pages from pager)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.output_dir).resolve()
    zip_path = Path(args.zip_path).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    driver = make_driver(headless=not args.headful)
    session: Optional[requests.Session] = None
    headless = not args.headful

    try:
        log("Initializing authenticated browser session...")
        driver, session = ensure_live_authenticated_session(driver, headless=headless)

        pdf_urls: Set[str] = set()
        for dataset in args.datasets:
            attempts = 0
            while attempts < 2:
                try:
                    pdf_urls |= collect_dataset_pdf_urls(
                        driver=driver,
                        dataset=dataset,
                        max_pages=args.max_pages,
                        refresh_before_each_page=args.refresh_before_each_page,
                        page_delay=args.page_delay,
                    )
                    break
                except DriverSessionLost:
                    attempts += 1
                    log(f"Dataset {dataset}: browser session lost, recreating browser ({attempts}/2)")
                    driver, session = ensure_live_authenticated_session(driver, headless=headless)
            if attempts >= 2:
                log(f"Dataset {dataset}: skipped after repeated browser session loss.")

        if not pdf_urls:
            log("No PDF links discovered.")
            return 1

        log(f"Filtering PDFs by marker text: {args.pdf_marker}")
        marker_urls: Set[str] = set()
        checked = 0
        for u in sorted(pdf_urls):
            checked += 1
            if checked == 1 or (args.refresh_every > 0 and checked % args.refresh_every == 0):
                driver, session = ensure_live_authenticated_session(driver, headless=headless)

            status = pdf_contains_marker(session, u, args.pdf_marker)
            if status == "refresh":
                driver, session = ensure_live_authenticated_session(driver, headless=headless)
                status = pdf_contains_marker(session, u, args.pdf_marker)

            if status == "hit":
                marker_urls.add(u)

        log(f"PDFs matching marker: {len(marker_urls)} / {len(pdf_urls)}")

        stems: List[str] = []
        seen: Set[str] = set()
        for u in sorted(marker_urls):
            stem = stem_from_pdf_url(u)
            if stem and stem not in seen:
                seen.add(stem)
                stems.append(stem)

        if args.max_stems > 0:
            stems = stems[: args.max_stems]

        log(f"Total unique stems: {len(stems)}")
        if not stems:
            log("No stems found. Exiting.")
            return 1

        manifest: List[Dict[str, str]] = []

        for idx, stem in enumerate(stems, start=1):
            if idx == 1 or (args.refresh_every > 0 and idx % args.refresh_every == 0):
                log("Refreshing access cookies...")
                driver, session = ensure_live_authenticated_session(driver, headless=headless)

            found = False
            for ext in args.extensions:
                candidate = requote_uri(stem + ext)
                result = download_candidate(session, candidate, out_dir, args.force, args.probe_only)

                if result and result.get("status") == "refresh":
                    log("Session challenged; re-authenticating and retrying...")
                    driver, session = ensure_live_authenticated_session(driver, headless=headless)
                    result = download_candidate(session, candidate, out_dir, args.force, args.probe_only)

                if result and result.get("status") == "ok":
                    manifest.append(result)
                    log(f"[{idx}/{len(stems)}] OK: {result['url']}")
                    found = True
                    break

            if not found:
                log(f"[{idx}/{len(stems)}] No video found for stem: {stem}")

        manifest_path = out_dir / "manifest.csv"
        write_manifest(manifest_path, manifest)
        log(f"Manifest written: {manifest_path}")

        if not args.probe_only:
            zip_output(out_dir, zip_path)
            log(f"ZIP created: {zip_path}")

        log(f"Done. Valid videos: {len(manifest)}")
        return 0

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
