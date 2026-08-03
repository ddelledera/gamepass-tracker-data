"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica le pagine di gg.deals con i giochi in arrivo/annunciati/in uscita
da Xbox Game Pass, le analizza, e salva tutto in un file JSON
(upcoming_data.json) che l'app Flutter scarica direttamente.

Pensato per girare automaticamente ogni giorno tramite GitHub Actions.
"""

import json
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

COMING_URL = "https://gg.deals/subscription-news/the-list-of-all-games-coming-to-game-pass/"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"

MONTHS_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}


def fetch_soup(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(text: str):
    """
    Prova a interpretare una data testuale (es. "August 4, 2026") in un
    oggetto datetime. Ritorna None se non è una data esatta riconoscibile
    (es. "TBC", "Q4 2026", "2026" da soli restano come testo libero).
    """
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def scrape_coming_and_announced():
    """
    Analizza la pagina "lista completa" di gg.deals, dividendo i giochi
    in due gruppi: con data (anche approssimativa) e annunciati senza data.
    """
    soup = fetch_soup(COMING_URL)

    with_date = []
    announced = []

    # La pagina è organizzata in blocchi con intestazioni (h2/h3) e liste
    # puntate sotto ciascuna. Cerchiamo le liste vicine alle intestazioni
    # che parlano di "coming" (con data) e "announced"/"TBC" (senza data).
    for heading in soup.find_all(re.compile("^h[1-4]$")):
        heading_text = heading.get_text(strip=True).lower()

        is_announced_section = "announced" in heading_text or "tbc" in heading_text
        is_coming_section = (
            "coming" in heading_text
            and "announced" not in heading_text
        )

        if not (is_announced_section or is_coming_section):
            continue

        # Cerchiamo la prima lista (ul) subito dopo questa intestazione.
        sibling = heading.find_next_sibling()
        steps = 0
        while sibling is not None and sibling.name != "ul" and steps < 6:
            sibling = sibling.find_next_sibling()
            steps += 1

        if sibling is None or sibling.name != "ul":
            continue

        for li in sibling.find_all("li"):
            raw_text = li.get_text(" ", strip=True)
            if not raw_text:
                continue

            if is_announced_section:
                # Niente data: solo il titolo del gioco.
                title = re.split(r"[-–—]", raw_text)[0].strip()
                if title:
                    announced.append({"title": title})
            else:
                # Proviamo a separare "Titolo - Data" (formati comuni).
                parts = re.split(r"[-–—]\s*", raw_text, maxsplit=1)
                title = parts[0].strip()
                date_text = parts[1].strip() if len(parts) > 1 else ""

                if not title:
                    continue

                parsed = parse_date(date_text) if date_text else None
                if parsed:
                    with_date.append({
                        "title": title,
                        "exactDate": parsed.strftime("%Y-%m-%d"),
                    })
                else:
                    with_date.append({
                        "title": title,
                        "approxLabel": date_text or "TBC",
                    })

    return with_date, announced


def scrape_leaving_soon():
    """
    Trova l'articolo più recente sulla pagina indice "giochi in uscita" e
    ne estrae l'elenco dei titoli. Più fragile delle altre funzioni perché
    si basa su un articolo di notizie, non su una lista sempre aggiornata.
    """
    index_soup = fetch_soup(LEAVING_INDEX_URL)

    # Cerchiamo il primo link che sembra un articolo (non un link di menu).
    article_link = None
    for a in index_soup.find_all("a", href=True):
        href = a["href"]
        if "/subscription-news/" in href and href.count("/") >= 4:
            article_link = href
            break

    if article_link is None:
        return []

    if article_link.startswith("/"):
        article_link = "https://gg.deals" + article_link

    article_soup = fetch_soup(article_link)

    leaving = []
    for li in article_soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        # Filtriamo righe troppo corte/lunghe per essere un nome di gioco.
        if 2 <= len(text) <= 80 and not text.lower().startswith("regular price"):
            leaving.append({"title": text})

    # Rimuoviamo eventuali doppioni mantenendo l'ordine.
    seen = set()
    unique_leaving = []
    for game in leaving:
        if game["title"] not in seen:
            seen.add(game["title"])
            unique_leaving.append(game)

    return unique_leaving[:20]  # Limite di sicurezza


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
