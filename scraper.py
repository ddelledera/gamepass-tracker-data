"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica le pagine di gg.deals con i giochi in arrivo/annunciati/in uscita
da Xbox Game Pass, usando un browser reale (Playwright) per aggirare il
blocco anti-bot che il sito applica alle richieste HTTP "semplici".
Salva tutto in upcoming_data.json, letto direttamente dall'app Flutter.
"""

import json
import re
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

COMING_URL = "https://gg.deals/subscription-news/the-list-of-all-games-coming-to-game-pass/"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"

_playwright = sync_playwright().start()
_browser = _playwright.chromium.launch()
_page = _browser.new_page(
    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_soup(url: str) -> BeautifulSoup:
    time.sleep(1)
    _page.goto(url, wait_until="networkidle", timeout=30000)
    html = _page.content()
    return BeautifulSoup(html, "html.parser")


def looks_like_game_title(text: str) -> bool:
    """Filtro per scartare 'rumore' tipico dei siti (FAQ, link di menu,
    testo promozionale) che finisce nei tag insieme ai veri titoli."""
    if not (2 <= len(text) <= 80):
        return False

    lowered = text.lower()
    if "?" in text:
        return False

    noise_starts = (
        "how to", "what is", "what are", "why", "when will", "where",
        "regular price", "read more", "compare prices", "best price",
        "buy ", "sign up", "subscribe", "follow us", "related",
    )
    if lowered.startswith(noise_starts):
        return False

    noise_contains = ("cd key", "activate", "cookie", "privacy policy")
    if any(phrase in lowered for phrase in noise_contains):
        return False

    return True


def clean_date_text(text: str) -> str:
    text = re.sub(r"\(\s*source\s*(,\s*source\s*)*\)", "", text, flags=re.IGNORECASE)
    return text.strip(" -–—\t")


def parse_date(text: str):
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def scrape_coming_and_announced():
    soup = fetch_soup(COMING_URL)

    with_date = []
    announced = []
    seen_titles = set()

    for heading in soup.find_all(re.compile("^h[1-4]$")):
        heading_text = heading.get_text(strip=True).lower()

        matches_keyword = "coming" in heading_text or "announced" in heading_text
        matches_year = re.search(r"\b20\d{2}\b", heading_text) is not None
        if not (matches_keyword or matches_year):
            continue

        lists_found = []
        sibling = heading.find_next_sibling()
        steps = 0
        while sibling is not None and not re.match(r"^h[1-4]$", sibling.name or "") and steps < 30:
            if sibling.name == "ul":
                lists_found.append(sibling)
            sibling = sibling.find_next_sibling()
            steps += 1

        for ul in lists_found:
            for li in ul.find_all("li"):
                raw_text = li.get_text(" ", strip=True)
                if not raw_text:
                    continue

                parts = re.split(r"\s[-–—]\s", raw_text, maxsplit=1)
                title = parts[0].strip()
                date_text = clean_date_text(parts[1]) if len(parts) > 1 else ""

                if (not title or title in seen_titles
                        or not looks_like_game_title(title)):
                    continue
                seen_titles.add(title)

                if not date_text or date_text.upper() == "TBC":
                    announced.append({"title": title})
                    continue

                parsed = parse_date(date_text)
                if parsed:
                    with_date.append({
                        "title": title,
                        "exactDate": parsed.strftime("%Y-%m-%d"),
                    })
                else:
                    with_date.append({"title": title, "approxLabel": date_text})

    print(f"[coming] Con data: {len(with_date)}, Annunciati: {len(announced)}")
    return with_date, announced


GAME_PASS_TAG_PATTERN = re.compile(r"^(.*?)\s*\(([^)]*Game Pass[^)]*)\)\s*$")


def get_article_publish_date(soup):
    """Cerca la data di pubblicazione reale dell'articolo, provando i
    posti più comuni dove i siti la mettono (meta tag standard)."""
    meta_names = [
        ("property", "article:published_time"),
        ("property", "article:modified_time"),
        ("name", "date"),
    ]
    for attr, value in meta_names:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            try:
                dt = datetime.fromisoformat(tag["content"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue

    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        try:
            dt = datetime.fromisoformat(time_tag["datetime"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

    return None


def scrape_leaving_soon():
    index_soup = fetch_soup(LEAVING_INDEX_URL)

    candidate_links = []
    for a in index_soup.find_all("a", href=True):
        href = a["href"]
        if "/subscription-news/" in href and "leav" in href.lower():
            if href not in candidate_links:
                candidate_links.append(href)

    print(f"[leaving] Trovati {len(candidate_links)} link candidati.")
    if not candidate_links:
        return []

    # Controlliamo la data di pubblicazione reale dei primi candidati
    # (non ci fidiamo dell'ordine in cui compaiono nella pagina) e
    # scegliamo quello più recente.
    checked = []
    for href in candidate_links[:4]:
        full_url = "https://gg.deals" + href if href.startswith("/") else href
        try:
            candidate_soup = fetch_soup(full_url)
        except Exception as e:
            print(f"[leaving] Impossibile controllare {full_url}: {e}")
            continue
        pub_date = get_article_publish_date(candidate_soup)
        print(f"[leaving] Candidato: {full_url} -> data: {pub_date}")
        checked.append((pub_date, full_url, candidate_soup))

    # Ordiniamo per data (i None finiscono per ultimi) e prendiamo il più recente.
    checked.sort(key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if not checked:
        return []

    _, article_link, article_soup = checked[0]
    print(f"[leaving] Uso l'articolo più recente: {article_link}")

    leaving = []
    for element in article_soup.find_all(["li", "p", "strong"]):
        text = element.get_text(" ", strip=True)
        match = GAME_PASS_TAG_PATTERN.match(text)
        if not match:
            continue

        title = match.group(1).strip(" -–—:")
        tiers = match.group(2).strip()

        if not looks_like_game_title(title) or len(title.split()) > 8:
            continue

        leaving.append({"title": title, "tiers": tiers})

    print(f"[leaving] Giochi riconosciuti tramite tag Game Pass: {len(leaving)}")

    seen = set()
    unique_leaving = []
    for game in leaving:
        if game["title"] not in seen:
            seen.add(game["title"])
            unique_leaving.append(game)

    return unique_leaving[:20]


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
