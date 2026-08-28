"""Scrape product data from books.toscrape.com."""

import csv
import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://books.toscrape.com/"
REQUEST_TIMEOUT = 10
OUTPUT_CSV = Path(__file__).resolve().parent / "books_data.csv"

BASE_COLUMNS = [
    "name",
    "price",
    "stock_availability",
    "image_url",
    "description",
    "url",
]

INFO_KEY_MAP = {
    "UPC": "info_upc",
    "Product Type": "info_product_type",
    "Price (excl. tax)": "info_price_excl_tax",
    "Price (incl. tax)": "info_price_incl_tax",
    "Tax": "info_tax",
    "Availability": "info_availability",
    "Number of reviews": "info_number_of_reviews",
}

CSV_FIELDNAMES = BASE_COLUMNS + list(INFO_KEY_MAP.values())

USER_AGENT = (
    "Mozilla/5.0 (compatible; BooksScraper/1.0; +https://books.toscrape.com/)"
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def create_session() -> requests.Session:
    """Return a requests Session with polite default headers."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_page(session: requests.Session, url: str) -> str | None:
    """GET a URL and return its text body, or None on failure."""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("Request failed for %s: %s", url, exc)
        return None

    if response.status_code != 200:
        logger.error("Non-200 status %s for %s", response.status_code, url)
        return None

    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("HTTP error for %s: %s", url, exc)
        return None

    return response.text


def iter_listing_page_urls() -> Iterator[str]:
    """Yield listing page URLs for books.toscrape.com (page 1, then pages 2–50)."""
    yield urljoin(BASE_URL, "index.html")
    for page_num in range(2, 51):
        yield urljoin(BASE_URL, f"catalogue/page-{page_num}.html")


def get_listing_page_urls() -> Iterator[str]:
    """Alias for iter_listing_page_urls (plan naming)."""
    yield from iter_listing_page_urls()


def get_next_listing_page_url(listing_html: str, current_url: str) -> str | None:
    """Return the next listing page URL, or None when pagination ends (no li.next)."""
    soup = BeautifulSoup(listing_html, "html.parser")
    next_link = soup.select_one("li.next a[href]")
    if next_link is None:
        return None
    return urljoin(current_url, next_link["href"])


def get_book_links(listing_html: str) -> list[str]:
    """Extract absolute book detail URLs from a listing page."""
    soup = BeautifulSoup(listing_html, "html.parser")
    links: list[str] = []
    for article in soup.select("article.product_pod"):
        anchor = article.select_one("h3 a[href]")
        if anchor and anchor.get("href"):
            links.append(urljoin(BASE_URL, anchor["href"]))
    return links


def parse_book_detail(html: str, url: str) -> dict:
    """Parse a book detail page into structured product data."""
    soup = BeautifulSoup(html, "html.parser")

    name_el = soup.select_one("motion-carousel h1") or soup.select_one(
        "div.product_main h1"
    )
    name = name_el.get_text(strip=True) if name_el else ""

    price_el = soup.select_one("p.price_color")
    price = price_el.get_text(strip=True) if price_el else ""

    stock_el = soup.select_one("p.instock.availability")
    stock_availability = stock_el.get_text(strip=True) if stock_el else ""

    img_el = soup.select_one("div.item.active img[src]")
    image_url = (
        urljoin(url, img_el["src"]) if img_el and img_el.get("src") else ""
    )

    description = ""
    desc_anchor = soup.select_one("#product_description")
    if desc_anchor:
        desc_p = desc_anchor.find_next_sibling("p")
        if desc_p:
            description = desc_p.get_text(strip=True)

    product_information: dict[str, str] = {}
    info_table = soup.select_one("table.table.table-striped")
    if info_table:
        for row in info_table.select("tr"):
            header = row.select_one("th")
            value = row.select_one("td")
            if header and value:
                product_information[header.get_text(strip=True)] = value.get_text(
                    strip=True
                )

    return {
        "name": name,
        "price": price,
        "stock_availability": stock_availability,
        "image_url": image_url,
        "description": description,
        "product_information": product_information,
        "url": url,
    }


def iter_listing_pages(_session: requests.Session) -> Iterator[str]:
    """Yield all listing page URLs for the main scrape loop."""
    yield from iter_listing_page_urls()


def collect_book_urls_from_listings(session: requests.Session) -> list[str]:
    """Walk all listing pages and return every absolute book detail URL."""
    book_urls: list[str] = []
    for listing_url in iter_listing_page_urls():
        html = fetch_page(session, listing_url)
        if html is None:
            logger.error("Skipping listing page %s", listing_url)
            continue
        book_urls.extend(get_book_links(html))
    return book_urls


def _info_key_to_column(key: str) -> str:
    """Convert a product_information key to a flattened CSV column name."""
    if key in INFO_KEY_MAP:
        return INFO_KEY_MAP[key]
    slug = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return f"info_{slug}"


def flatten_book_for_csv(book: dict) -> dict[str, str]:
    """Flatten a parsed book dict into a single CSV row."""
    row = {column: book.get(column, "") for column in BASE_COLUMNS}
    for key, value in book.get("product_information", {}).items():
        row[_info_key_to_column(key)] = value
    return row


class BookCsvWriter:
    """Write book rows incrementally to CSV with a header written once."""

    def __init__(self, path: Path = OUTPUT_CSV) -> None:
        self.path = path
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._file, fieldnames=CSV_FIELDNAMES, extrasaction="ignore"
        )
        self._writer.writeheader()
        self._file.flush()

    def write_row(self, book: dict) -> None:
        self._writer.writerow(flatten_book_for_csv(book))
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def print_summary(books_saved: int, error_count: int, books: list[dict] | None = None) -> None:
    """Print the final scrape summary to stdout."""
    print("Scraping complete.")
    print(f"Books saved: {books_saved}")
    print(f"Errors: {error_count}")
    print(f"Output: {OUTPUT_CSV}")
    if books:
        print("\nFirst 3 books:")
        for book in books[:3]:
            print(f"  - {book.get('name', '')} | {book.get('price', '')}")


def scrape_books(max_listing_pages: int | None = None) -> tuple[list[dict], list[str], bool]:
    """Scrape books from listing pages and write each row to CSV incrementally."""
    session = create_session()
    all_books: list[dict] = []
    errors: list[str] = []
    csv_writer = BookCsvWriter()
    interrupted = False

    try:
        listing_urls = list(iter_listing_pages(session))
        if max_listing_pages is not None:
            listing_urls = listing_urls[:max_listing_pages]

        for listing_url in listing_urls:
            listing_html = fetch_page(session, listing_url)
            if listing_html is None:
                errors.append(listing_url)
                continue

            for book_url in get_book_links(listing_html):
                detail_html = fetch_page(session, book_url)
                if detail_html is None:
                    errors.append(book_url)
                    continue

                try:
                    book = parse_book_detail(detail_html, book_url)
                    all_books.append(book)
                    csv_writer.write_row(book)
                except Exception as exc:
                    logger.error("Parse failed for %s: %s", book_url, exc)
                    errors.append(f"{book_url}: {exc}")
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Scraping interrupted; partial CSV saved.")
    finally:
        csv_writer.close()

    return all_books, errors, interrupted


def main() -> None:
    """Run the full scrape and print a summary."""
    try:
        all_books, errors, interrupted = scrape_books()
    except Exception as exc:
        logger.exception("Unexpected error during scraping: %s", exc)
        print(f"Scraping failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if interrupted:
        print("\nScraping interrupted. Partial results were saved.", file=sys.stderr)

    print_summary(len(all_books), len(errors), all_books)

    if interrupted:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
