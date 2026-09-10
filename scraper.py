"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica da xboxgamepasslist.com (un database indipendente dedicato al
catalogo Xbox Game Pass) le liste di giochi "Coming Soon" e "Leaving
Soon", le analizza, e salva tutto in un file JSON (upcoming_data.json)
che l'app Flutter scarica direttamente.

Pensato per girare automaticamente più volte al giorno tramite
GitHub Actions.
"""

import json
import re
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

BASE_URL = "https://www.xboxgamepasslist.com/"
MAX_PAGES = 6  # limite di sicurezza per evitare loop infiniti

# Un solo browser condiviso per tutto lo script, per velocità.
_playwright = sync_playwright().start()
_browser = _playwright.chromium.launch()
_page = _browser.new_page(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_soup(url: str) -> BeautifulSoup:
    time.sleep(1)
    _page.goto(url, wait_until="networkidle", timeout=30000)
    # Aspettiamo che la tabella dei giochi compaia davvero nella pagina
    # (viene creata da JavaScript dopo il caricamento iniziale).
    try:
        _page.wait_for_selector("table", timeout=10000)
    except Exception:
        pass  # se non compare, il parsing sotto restituirà 0 righe
    html = _page.content()
    return BeautifulSoup(html, "html.parser")


def looks_like_game_title(text: str) -> bool:
    if not (2 <= len(text) <= 100):
        return False
    lowered = text.lower()
    if "?" in text:
        return False
    noise_starts = ("how to", "what is", "what are", "why", "when will")
    if lowered.startswith(noise_starts):
        return False
    return True


def parse_exact_date(text: str):
    """Es. 'Oct 6, 2026' -> datetime. Ritorna None se non è una data
    esatta (es. 'TBA')."""
    text = text.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_table_rows(soup):
    """Analizza la tabella principale della pagina e restituisce una
    lista di dizionari grezzi: title, status, added, leaving."""
    rows_data = []
    table = soup.find("table")
    if table is None:
        return rows_data

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        game_cell = cells[0]
        link = game_cell.find("a")
        if link is None:
            continue
        title = link.get_text(strip=True)
        if not title or not looks_like_game_title(title):
            continue

        status = cells[4].get_text(strip=True)
        added = cells[5].get_text(strip=True)
        leaving = cells[6].get_text(strip=True)

        rows_data.append({
            "title": title,
            "status": status,
            "added": added,
            "leaving": leaving,
        })

    return rows_data


def fetch_all_pages(status_filter: str, extra_query: str = ""):
    """Scarica tutte le pagine di risultati per un filtro di stato,
    seguendo la paginazione finché ce n'è (fino a MAX_PAGES)."""
    all_rows = []
    for page in range(1, MAX_PAGES + 1):
        if page == 1:
            url = f"{BASE_URL}?status={status_filter}{extra_query}"
        else:
            url = f"{BASE_URL}?status={status_filter}{extra_query}&page={page}"

        try:
            soup = fetch_soup(url)
        except Exception as e:
            print(f"[{status_filter}] Errore pagina {page}: {e}")
            break

        rows = parse_table_rows(soup)
        print(f"[{status_filter}] Pagina {page}: {len(rows)} righe trovate.")
        if not rows:
            break
        all_rows.extend(rows)

        # Se non c'è un link "Next" nella pagina, ci fermiamo qui.
        next_link = soup.find("a", string=re.compile(r"^\s*Next\s*$", re.IGNORECASE))
        if next_link is None:
            break

    return all_rows


def scrape_coming_and_announced():
    rows = fetch_all_pages("COMING_SOON")

    with_date = []
    announced = []
    seen = set()

    for row in rows:
        title = row["title"]
        if title in seen:
            continue
        seen.add(title)

        added = row["added"]
        if not added or added.upper() == "TBA":
            announced.append({"title": title})
            continue

        parsed = parse_exact_date(added)
        if parsed:
            with_date.append({
                "title": title,
                "exactDate": parsed.strftime("%Y-%m-%d"),
            })
        else:
            with_date.append({"title": title, "approxLabel": added})

    print(f"[coming] Con data: {len(with_date)}, Annunciati: {len(announced)}")
    return with_date, announced


def scrape_leaving_soon():
    rows = fetch_all_pages("LEAVING_SOON", extra_query="&sort=leaving-soon")

    leaving = []
    seen = set()

    for row in rows:
        title = row["title"]
        if title in seen:
            continue
        seen.add(title)

        leaving_date = row["leaving"]
        if not leaving_date or leaving_date.upper() == "TBA":
            continue  # senza data non è utile in questa sezione

        parsed = parse_exact_date(leaving_date)
        if parsed:
            leaving.append({
                "title": title,
                "exactDate": parsed.strftime("%Y-%m-%d"),
            })
        else:
            leaving.append({"title": title, "approxLabel": leaving_date})

    print(f"[leaving] Trovati: {len(leaving)}")
    return leaving[:30]  # limite di sicurezza


def main():
    try:
        with_date, announced = scrape_coming_and_announced()
    except Exception as e:
        print(f"Errore nello scraping 'coming/announced': {e}")
        with_date, announced = [], []

    try:
        leaving = scrape_leaving_soon()
    except Exception as e:
        print(f"Errore nello scraping 'leaving soon': {e}")
        leaving = []

    data = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "upcomingWithDate": with_date,
        "upcomingAnnounced": announced,
        "leavingSoon": leaving,
    }

    with open("upcoming_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Salvati: {len(with_date)} con data, {len(announced)} annunciati, "
          f"{len(leaving)} in uscita.")

    _browser.close()
    _playwright.stop()


if __name__ == "__main__":
    main()
