"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica le pagine di gg.deals con i giochi in arrivo/annunciati/in uscita
da Xbox Game Pass, instradando le richieste tramite ZenRows (un servizio
che risolve le protezioni anti-bot tipo Cloudflare) per aggirare il
blocco che impediva le richieste dirette. Salva tutto in
upcoming_data.json, letto direttamente dall'app Flutter.

La chiave API di ZenRows viene letta dalla variabile d'ambiente
ZENROWS_API_KEY (impostata come "secret" nel workflow GitHub Actions,
non scritta qui nel codice).
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "")
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"

COMING_URL = "https://gg.deals/subscription-news/the-list-of-all-games-coming-to-game-pass/"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"


def fetch_soup(url: str) -> BeautifulSoup:
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY non impostata.")

    time.sleep(1)
    params = {
        "url": url,
        "apikey": ZENROWS_API_KEY,
        "js_render": "true",
        "premium_proxy": "true",
        "wait": "3000",  # aspetta 3 secondi extra dopo il caricamento
    }
    response = requests.get(ZENROWS_ENDPOINT, params=params, timeout=90)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def looks_like_game_title(text: str) -> bool:
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


ENTRY_PATTERN = re.compile(r"^(.+?)\s*[-–—]\s*(.+?)\s*\(\s*source", re.IGNORECASE)


def scrape_coming_and_announced():
    soup = fetch_soup(COMING_URL)

    page_text = soup.get_text(" ", strip=True)
    print(f"[coming] Lunghezza testo pagina: {len(page_text)} caratteri.")
    print(f"[coming] Titolo pagina: {soup.title.get_text(strip=True) if soup.title else 'ASSENTE'}")
    print(f"[coming] Contiene 'Game Pass': {'Game Pass' in page_text}")
    print(f"[coming] Contiene '(source': {'(source' in page_text.lower()}")
    print(f"[coming] Elementi li/p/strong trovati: {len(soup.find_all(['li', 'p', 'strong']))}")

    with_date = []
    announced = []
    seen_titles = set()

    # Ogni riga vera ha il formato "Titolo – Data (source...)": cerchiamo
    # questo schema in tutti gli elementi di testo della pagina, senza
    # dipendere dalla struttura esatta (tag, intestazioni, liste) usata
    # dal sito, che potrebbe non corrispondere a quella "visibile".
    for element in soup.find_all(["li", "p", "strong"]):
        raw_text = element.get_text(" ", strip=True)
        if not raw_text or "(source" not in raw_text.lower():
            continue

        match = ENTRY_PATTERN.match(raw_text)
        if not match:
            continue

        title = match.group(1).strip(" -–—:")
        date_text = clean_date_text(match.group(2))

        if not title or title in seen_titles or not looks_like_game_title(title):
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
        if "/subscription-news/" in href and (
            "leav" in href.lower() or "losing" in href.lower()
        ):
            if href not in candidate_links:
                candidate_links.append(href)

    print(f"[leaving] Trovati {len(candidate_links)} link candidati.")
    if not candidate_links:
        return []

    checked = []
    for href in candidate_links[:5]:
        full_url = "https://gg.deals" + href if href.startswith("/") else href
        try:
            candidate_soup = fetch_soup(full_url)
        except Exception as e:
            print(f"[leaving] Impossibile controllare {full_url}: {e}")
            continue
        pub_date = get_article_publish_date(candidate_soup)
        print(f"[leaving] Candidato: {full_url} -> data: {pub_date}")
        checked.append((pub_date, full_url, candidate_soup))

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
    with_date, announced = [], []
    for attempt in range(1, 3):
        try:
            with_date, announced = scrape_coming_and_announced()
        except Exception as e:
            print(f"Errore nello scraping 'coming/announced' (tentativo {attempt}): {e}")
        if with_date or announced:
            break
        print(f"[coming] Risultato vuoto al tentativo {attempt}, riprovo...")
        time.sleep(5)

    leaving = []
    for attempt in range(1, 3):
        try:
            leaving = scrape_leaving_soon()
        except Exception as e:
            print(f"Errore nello scraping 'leaving soon' (tentativo {attempt}): {e}")
        if leaving:
            break
        print(f"[leaving] Risultato vuoto al tentativo {attempt}, riprovo...")
        time.sleep(5)

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
