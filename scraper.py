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
XGPL_BASE_URL = "https://www.xboxgamepasslist.com/"
XGPL_MAX_PAGES = 15
PUREXBOX_URL = "https://www.purexbox.com/guides/xbox-game-pass-in-2026-the-full-list-of-everything-announced-so-far"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"
WAVE_INDEX_URL = "https://gg.deals/news/subscriptions/"


def fetch_soup_direct(url: str) -> BeautifulSoup:
    """Richiesta HTTP semplice, senza passare da ZenRows: piu' veloce e
    gratuita, da provare per prima sui siti che potrebbero non avere
    bisogno di un servizio anti-bot dedicato."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def fetch_soup(url: str, js_render: bool = True, antibot: bool = False) -> BeautifulSoup:
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY non impostata.")

    time.sleep(1)
    params = {
        "url": url,
        "apikey": ZENROWS_API_KEY,
        "premium_proxy": "true",
    }
    if js_render:
        params["js_render"] = "true"
        params["wait"] = "3000"
    if antibot:
        params["antibot"] = "true"

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


COMING_STATUS_PATTERN = re.compile(
    r"Coming\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
    r"\s+\d{1,2},?\s+\d{4}|TBA)",
    re.IGNORECASE,
)



def parse_xgpl_date(text: str):
    text = text.strip().replace(",", "")
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


TIER_SPLIT_PATTERN = re.compile(
    r"(?=Ultimate|Premium|Essential|Ea Play|Game Catalog)"
)

# Voci del menu/interfaccia del sito osservate direttamente nei risultati
# reali: non sono giochi, vanno scartate a prescindere.
XGPL_NAV_NOISE = {
    "games list", "new", "leaving soon", "best", "plans", "calendar",
    "blog", "search games", "pc games", "cloud games", "all games table",
    "popular searches", "catalog guide", "new and leaving",
    "plan and platform guide", "faq", "new this month", "coming soon",
    "console games", "best rated", "short games", "console games",
    "pc game pass games", "cloud games", "ultimate games", "premium games",
    "essential games", "day-one games", "compare plans", "xbox catalog",
    "xbox wire", "about", "contact", "privacy", "terms", "sitemap",
    "view new games", "view coming soon", "view leaving soon",
    "best games guide", "leaving soon tracker", "new games",
}


def clean_xgpl_title(raw_title: str) -> str:
    """Rimuove le informazioni di piano/piattaforma che il sito attacca
    senza spazio subito dopo il titolo (es. 'Cricket 24Ultimate, ...')."""
    return TIER_SPLIT_PATTERN.split(raw_title, maxsplit=1)[0].strip()


def extract_xgpl_games(soup):
    results = []
    seen_titles = set()

    for link in soup.find_all("a"):
        raw_text = link.get_text(strip=True)
        if not raw_text:
            continue

        # Cerchiamo lo stato "Coming <data>"/"Coming TBA" DIRETTAMENTE nel
        # testo di questo stesso link: card e informazioni sono tutte
        # concatenate insieme in un unico <a>, quindi non serve (ed è
        # rischioso) risalire ai contenitori genitori.
        status_match = COMING_STATUS_PATTERN.search(raw_text)
        if status_match is None:
            continue
        date_text = status_match.group(1)

        title = clean_xgpl_title(raw_text)
        if not title or title in seen_titles or not looks_like_game_title(title):
            continue
        if title.lower() in XGPL_NAV_NOISE:
            continue

        seen_titles.add(title)
        results.append({"title": title, "date_text": date_text})

    return results


def fetch_xgpl_pages(status_filter: str, extra_query: str = ""):
    all_games = []
    consecutive_link_less_pages = 0

    for page in range(1, XGPL_MAX_PAGES + 1):
        if page == 1:
            url = f"{XGPL_BASE_URL}?status={status_filter}{extra_query}"
        else:
            url = f"{XGPL_BASE_URL}?status={status_filter}{extra_query}&page={page}"

        try:
            soup = fetch_soup(url, js_render=True)
        except Exception as e:
            print(f"[coming] Errore pagina {page}: {e}")
            break

        total_links = len(soup.find_all("a"))
        games = extract_xgpl_games(soup)
        print(f"[coming] Pagina {page}: {len(games)} giochi trovati "
              f"({total_links} link totali sulla pagina).")
        all_games.extend(games)

        # Non ci fidiamo di riconoscere un pulsante "pagina successiva"
        # specifico (potrebbe essere un'icona, non testo "Next"): ci
        # fermiamo solo se la pagina risulta praticamente vuota per due
        # volte di fila, segno che abbiamo superato l'ultima pagina vera.
        if total_links < 10:
            consecutive_link_less_pages += 1
            if consecutive_link_less_pages >= 2:
                break
        else:
            consecutive_link_less_pages = 0

    return all_games

    return all_games


def parse_purexbox_date(date_text: str, today_year: int):
    """Analizza le date della tabella Pure Xbox nei loro vari formati:
    'January 6, 2026' (esatta), 'June 2026 TBD' (mese+anno), 'December
    2026' (mese+anno), 'TBD' (nessuna data)."""
    text = date_text.strip()

    if text.upper() == "TBD":
        return None, None

    # Formato "Month YYYY TBD" -> trattiamo come approssimativa
    cleaned = re.sub(r"\s*TBD\s*$", "", text, flags=re.IGNORECASE).strip()

    for fmt in ("%B %d, %Y", "%B %d. %Y"):
        try:
            return datetime.strptime(cleaned, fmt), None
        except ValueError:
            continue

    # Formato "Month YYYY" (solo mese e anno, senza giorno preciso)
    for fmt in ("%B %Y",):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return None, cleaned  # lo trattiamo come approssimativo
        except ValueError:
            continue

    return None, cleaned if cleaned else None


def scrape_coming_and_announced():
    """
    Usiamo la guida di Pure Xbox (sito di notizie con una tabella
    Data/Gioco/Piattaforma aggiornata regolarmente), instradata tramite
    ZenRows. E' una pagina "normale" da sito di notizie, molto piu'
    semplice da leggere in modo affidabile delle web-app complesse
    provate in precedenza.
    """
    try:
        soup = fetch_soup_direct(PUREXBOX_URL)
        print("[coming] Richiesta semplice riuscita (senza ZenRows).")
    except Exception as e:
        print(f"[coming] Richiesta semplice fallita ({e}), riprovo con ZenRows...")
        soup = fetch_soup(PUREXBOX_URL, js_render=True, antibot=True)

    table = soup.find("table")
    if table is None:
        print("[coming] Nessuna tabella trovata nella pagina.")
        return [], []

    today = datetime.now(timezone.utc).replace(tzinfo=None)
    with_date = []
    announced = []
    seen = set()

    rows = table.find_all("tr")
    print(f"[coming] Righe trovate nella tabella: {len(rows)}")

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        date_text = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)

        if not title or not date_text or not looks_like_game_title(title):
            continue
        if title in seen:
            continue
        seen.add(title)

        if date_text.upper() == "TBD":
            announced.append({"title": title})
            continue

        exact_date, approx_label = parse_purexbox_date(date_text, today.year)

        if exact_date:
            if exact_date.date() < today.date():
                continue  # gia' uscito, non e' "in arrivo"
            with_date.append({
                "title": title,
                "exactDate": exact_date.strftime("%Y-%m-%d"),
            })
        elif approx_label:
            with_date.append({"title": title, "approxLabel": approx_label})
        else:
            announced.append({"title": title})

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
