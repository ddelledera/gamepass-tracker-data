"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica da xboxgamepasslist.com le liste di giochi "Coming Soon" e
"Leaving Soon", le analizza, e salva tutto in upcoming_data.json.

Invece di assumere una struttura HTML precisa (es. un tag <table>),
cerca direttamente i link dei titoli dei giochi e la prima data
riconoscibile nel testo del loro contenitore più vicino: un approccio
più robusto, che non si rompe se il sito cambia leggermente
l'impaginazione.
"""

import json
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_URL = "https://www.xboxgamepasslist.com/"
MAX_PAGES = 6

DATE_PATTERN = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"\d{1,2},?\s+\d{4}\b|\bTBA\b",
    re.IGNORECASE,
)

_session = requests.Session()
_session.headers.update(HEADERS)


def fetch_soup(url: str) -> BeautifulSoup:
    time.sleep(2)
    response = _session.get(url, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def looks_like_game_title(text: str) -> bool:
    if not (2 <= len(text) <= 100):
        return False
    if "?" in text:
        return False
    lowered = text.lower()
    noise_starts = ("how to", "what is", "what are", "why", "when will",
                     "home", "sign in", "log in", "menu")
    if lowered.startswith(noise_starts):
        return False
    if lowered in ("next", "previous", "coming soon", "leaving soon"):
        return False
    return True


def find_nearest_date(link_tag) -> str | None:
    """Risale i contenitori "genitori" del link del titolo, finché non
    trova un testo con una data riconoscibile (o 'TBA')."""
    node = link_tag
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        match = DATE_PATTERN.search(text)
        if match:
            return match.group(0)
    return None


def parse_exact_date(text: str):
    text = text.strip().replace(",", "")
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def extract_games_with_dates(soup):
    results = []
    seen_titles = set()

    for link in soup.find_all("a"):
        title = link.get_text(strip=True)
        if not title or title in seen_titles or not looks_like_game_title(title):
            continue

        date_text = find_nearest_date(link)
        if date_text is None:
            continue  # niente data vicina: probabilmente non è una riga di gioco

        seen_titles.add(title)
        results.append({"title": title, "date_text": date_text})

    return results


def fetch_all_pages(status_filter: str, extra_query: str = ""):
    all_games = []
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

        games = extract_games_with_dates(soup)
        print(f"[{status_filter}] Pagina {page}: {len(games)} giochi trovati.")
        if not games:
            break
        all_games.extend(games)

        next_link = soup.find("a", string=re.compile(r"^\s*Next\s*$", re.IGNORECASE))
        if next_link is None:
            break

    return all_games


def scrape_coming_and_announced():
    games = fetch_all_pages("COMING_SOON")

    with_date = []
    announced = []
    seen = set()

    for game in games:
        title = game["title"]
        if title in seen:
            continue
        seen.add(title)

        date_text = game["date_text"]
        if date_text.upper() == "TBA":
            announced.append({"title": title})
            continue

        parsed = parse_exact_date(date_text)
        if parsed:
            with_date.append({
                "title": title,
                "exactDate": parsed.strftime("%Y-%m-%d"),
            })
        else:
            with_date.append({"title": title, "approxLabel": date_text})

    print(f"[coming] Con data: {len(with_date)}, Annunciati: {len(announced)}")
    return with_date, announced


def scrape_leaving_soon():
    games = fetch_all_pages("LEAVING_SOON", extra_query="&sort=leaving-soon")

    leaving = []
    seen = set()

    for game in games:
        title = game["title"]
        if title in seen:
            continue
        seen.add(title)

        date_text = game["date_text"]
        if date_text.upper() == "TBA":
            continue

        parsed = parse_exact_date(date_text)
        if parsed:
            leaving.append({
                "title": title,
                "exactDate": parsed.strftime("%Y-%m-%d"),
            })
        else:
            leaving.append({"title": title, "approxLabel": date_text})

    print(f"[leaving] Trovati: {len(leaving)}")
    return leaving[:30]


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


if __name__ == "__main__":
    main()
