# 🛒 Grocery Price Comparator

Compare prices across **Maxi**, **Provigo**, **Metro**, and **Super C** in Montreal.

## Quick Start

**1. Install Python dependencies**
```
pip install -r requirements.txt
```

**2. Run the app**
```
python app.py
```
Your browser will open automatically at `http://localhost:5000`.

---

## How to Use

1. **Set up your stores** — Enter your postal code (e.g. `H3A 1B1`) and click *Find Stores*. Pick the Maxi and Provigo nearest you.

2. **Enter your grocery list** — Type items (one per line) or upload a `.txt` file.

3. **Click Compare Prices** — Wait ~15–25 seconds while the app searches all four stores in parallel.

Results show the top matching product at each store, with the best price highlighted in green.

---

## Store Notes

| Store | How prices are fetched |
|-------|------------------------|
| Maxi | PC Express API — very reliable |
| Provigo | PC Express API — very reliable |
| Metro | metro.ca HTML scraping — may need JS workaround (see below) |
| Super C | superc.ca HTML scraping — may need JS workaround (see below) |

### If Metro / Super C show "N/A"

Metro and Super C use JavaScript-heavy websites. If the scraper can't parse their pages automatically, you can add Playwright support:

```
pip install playwright
playwright install chromium
```

Then in `app.py`, replace the `search_metro_superc` function call with a Playwright-based version. Example in comments at the bottom of `app.py`.

### Loblaw "Could not load prices" on a few items (multi-buy / promo offers)

Most Maxi/Provigo items are priced through the PC Express product-detail API
(`.../pcx-bff/api/v1/products/{code}`). A handful of in-stock SKUs — observed on
multi-buy / promotional "offer" items (e.g. "$5.00 MIN 2") — return a hard **404**
from that endpoint even though the product page and search results show a price.
It is not auth, store, or stock related, and the code is not stale; the price
simply lives in the search/listing layer for these SKUs, not the detail endpoint.

To handle this, `search_store_by_urls` has a **fallback**: when the detail API
returns nothing after its normal retries, `_fetch_loblaw_price_via_search()`
opens the store's search page (Playwright, using the logged-in Firefox profile),
scrapes the result tiles, and matches the tile whose link carries the **exact**
product code. If the code isn't found it returns nothing (reports "couldn't load")
rather than guessing a wrong price. This path only runs for items that fail the
fast API, so normal items are unaffected — but those specific items resolve a bit
slower because each launches a headless browser. If the search page's tile
markup changes, update the selectors in `_LOBLAW_SEARCH_JS` (currently keyed on
`a[href*="/p/"]`, `data-testid="product-title"/"product-brand"/"product-package-size"`).

---

## How the "Best" price is chosen

The green **✓ Best** badge marks the best *value*, not the lowest sticker price.

- When every store in a row reports a **per-unit price** (per lb, per kg, per 100 g,
  or per 100 ml), stores are compared by that per-unit price. So a larger bottle
  that costs more overall but less per 100 ml wins over a smaller, cheaper one —
  the point is to find the best deal across **different sizes**.
- The cell still **displays** the total price you'd pay, with the per-unit figure
  shown underneath as the reason it's flagged best.
- **Safety guard:** if some stores in a row have a per-unit price and others don't,
  the comparison falls back to **total price** for that row, so a per-100 ml value
  is never compared against a whole-package price. `per kg` is normalized to
  `per lb` before comparing.

This logic lives in `_make_result()` (`app.py`, sets `comparison_price`) and the
shared `rowBasis()` helper in `templates/index.html` (used by the results table,
CSV export, and shopping list).

---

## Grocery List File Format

A plain `.txt` file, one item per line:

```
milk
bread
eggs
chicken breasts
cheddar cheese
orange juice
```

---

## Tips

- Use **generic terms** ("milk", "eggs") rather than brand names for broader matches.
- Items are searched in parallel — more items ≠ much longer wait.
- Click any price in the results table to open that product page.
