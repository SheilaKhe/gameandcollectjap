import os
import re
import requests
import argparse
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus, urlparse, urlunparse, urlencode, parse_qs

app = Flask(__name__)

SEARCH_BASE = "https://www.cardmarket.com/fr/Pokemon/Products/Search?category=-1&searchString="
DEFAULT_FILTERS = {
    "sellerCountry": "12",  # France
    "language": "2",        # Français
    "minCondition": "2",    # Near Mint
}
PRICE_TREND_ANCHORS = [
    "tendance des prix", "prix moyen", "articles disponibles",
    "price trend", "average price", "available items",
    "preistrend", "durchschnittspreis", "verfügbare artikel",
    "andamento del prezzo", "prezzo medio", "articoli disponibili",
    "tendencia de precios", "precio medio", "artículos disponibles",
    "prijstrend", "gemiddelde prijs", "beschikbare artikelen",
]
PRICE_REGEX = re.compile(r"(?:\d{1,3}(?:[.,]\d{3})*|\d+)(?:[.,]\d{2})?\s*€")

def parse_cookie_header(cookie_header: str) -> dict:
    cookies = {}
    for part in cookie_header.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies

def make_session():
    cookie_env = os.getenv("COOKIE_CARDMARKET")
    cookies = parse_cookie_header(cookie_env) if cookie_env else {}

    try:
        import cloudscraper
        sess = cloudscraper.create_scraper(browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False
        })
        print("🟢 cloudscraper activé")
    except Exception as e:
        print(f"🔴 cloudscraper failed: {e}, fallback vers requests")
        sess = requests.Session()

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.cardmarket.com/",
    }
    sess.headers.update(headers)
    if cookies:
        sess.cookies.update(cookies)
    return sess

def add_filters(base_url: str, filters: dict) -> str:
    parts = list(urlparse(base_url))
    query = parse_qs(parts[4])
    for k, v in filters.items():
        query[k] = [v]
    parts[4] = urlencode(query, doseq=True)
    return urlunparse(parts)

def find_product_url(card_id: str, sess: requests.Session, timeout: int = 30) -> str:
    url = SEARCH_BASE + quote_plus(card_id)
    r = sess.get(url, allow_redirects=True, timeout=timeout)
    if "/Products/Singles/" in r.url:
        return r.url
    if r.status_code == 403:
        raise SystemExit("403 Forbidden : accès bloqué.")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for sel in [
        "table#ProductsTable a[href*='/Products/Singles/']",
        "div#ProductsTable a[href*='/Products/Singles/']",
        "a[href*='/Products/Singles/']",
    ]:
        a = soup.select_one(sel)
        if a and a.get("href"):
            return urljoin("https://www.cardmarket.com", a.get("href"))
    raise RuntimeError(f"Aucun produit trouvé pour '{card_id}'.")

def smallest_common_ancestor_with_keywords(node, keywords: list[str]):
    cur = node
    while cur is not None:
        text = cur.get_text(" ", strip=True).casefold()
        if any(kw in text for kw in keywords):
            return cur
        cur = cur.parent
    return None

def extract_lowest_price(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in PRICE_TREND_ANCHORS:
        el = soup.find(string=lambda t: isinstance(t, str) and anchor in t.casefold())
        if el:
            container = smallest_common_ancestor_with_keywords(el.parent or el, PRICE_TREND_ANCHORS)
            if not container:
                container = el.parent or el
            prices = []
            for s in container.find_all(string=lambda t: isinstance(t, str) and "€" in t):
                m = PRICE_REGEX.search(s)
                if m:
                    prices.append(m.group(0).strip())
            if prices:
                return prices[0]
    for s in soup.find_all(string=lambda t: isinstance(t, str) and "€" in t):
        m = PRICE_REGEX.search(s)
        if m:
            return m.group(0).strip()
    return None

def extract_median_price(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    offers_container = soup.find("div", class_="table-body")
    if not offers_container:
        return None
    rows = offers_container.find_all("div", recursive=False)
    if not rows:
        rows = offers_container.find_all("div", class_="article-row")
    if not rows:
        return None
    median_index = (len(rows) + 1) // 2 - 1
    if median_index < 0:
        median_index = 0
    median_row = rows[median_index]
    price_texts = [s.strip() for s in median_row.stripped_strings if "€" in s]
    return price_texts[0] if price_texts else None

def get_prices_for_query(card_id: str):
    sess = make_session()
    product_url = find_product_url(card_id, sess=sess, timeout=30)
    filtered_url = add_filters(product_url, DEFAULT_FILTERS)
    html_lowest = sess.get(filtered_url, timeout=30).text
    lowest = extract_lowest_price(html_lowest)

    median_filters = {
        "sellerCountry": "12", "sellerType": "1", "language": "2", "minCondition": "2"
    }
    median_url = add_filters(product_url, median_filters)
    html_median = sess.get(median_url, timeout=30).text
    median = extract_median_price(html_median)

    return lowest or "N/A", median or "N/A", filtered_url

# === FLASK ROUTES ===
@app.route("/getPrices", methods=["POST"])
def get_prices():
    data = request.get_json()
    query = data.get("query")
    if not query:
        return jsonify({"error": "Missing 'query' field"}), 400
    try:
        lowest, median, url = get_prices_for_query(query)
        return jsonify({
            "lowest": str(lowest),
            "median": str(median),
            "url": str(url)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def home():
    return "Flask app is running on Railway!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)