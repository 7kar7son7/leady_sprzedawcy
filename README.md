# Allegro Seller Bot (`ledy_sprzedawcy`)

Bot zbiera **publiczne** dane sprzedawców z Allegro (sekcja o sprzedającym na ofercie):
login, firma, NIP, telefon, e-mail, adres.

## Jak oglądać leady

Po uruchomieniu wynik jest w:

| Plik | Opis |
|------|------|
| `data/sellers.csv` | Excel / Google Sheets / LibreOffice |
| `data/sellers.sqlite` | SQLite (DB Browser, DBeaver) |

**CSV lokalnie (Windows):** otwórz `F:\ledy_sprzedawcy\data\sellers.csv` w Excelu  
albo w PowerShell:

```powershell
Import-Csv .\data\sellers.csv | Format-Table login, phone, email, company_name -AutoSize
```

Kolumny: `login`, `company_name`, `phone`, `email`, `nip`, `address`, `seller_url`, `offer_url`, `query`, `raw_note`.

## Instalacja

```powershell
cd F:\ledy_sprzedawcy   # albo katalog na serwerze
python -m venv .venv
# Windows:
.\.venv\Scripts\activate
# Linux:
# source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
copy config.example.yaml config.yaml   # Linux: cp
```

Na Linuxie z Chrome systemowym:

```bash
playwright install-deps chromium
# albo zainstaluj google-chrome / chromium
```

## Uruchomienie

**Z oknem przeglądarki** (zalecane – Allegro ma Captchę DataDome):

```powershell
python main.py --query "wiertarka" --pages 3 --limit 50
```

1. Otworzy się Chrome (`browser_profile/`).
2. Jeśli Captcha – przejdź ją ręcznie w oknie.
3. Bot zbiera sprzedawców z listingu i wyciąga publiczny kontakt z ofert.
4. Zapis: `data/sellers.csv` + `data/sellers.sqlite`.

Kolejne runy zwykle bez Captchy (cookie w profilu). Znani sprzedawcy z CSV są pomijani.

## Hosting / Docker

Kontener **nie ma X11** (`Missing X server or $DISPLAY`). Bot wykrywa to i wymusza **headless** + Chromium Playwright (bez systemowego Chrome).

W `config.yaml` na hostingu:

```yaml
headless: true
use_system_chrome: false
```

```bash
git clone https://github.com/7kar7son7/leady_sprzedawcy.git
cd leady_sprzedawcy
docker build -t leady-sprzedawcy .
docker run --rm -v "$PWD/data:/app/data" leady-sprzedawcy \
  python main.py --query "wiertarka" --pages 2 --limit 50
```

Albo bez Dockera: `pip install -r requirements.txt && playwright install --with-deps chromium`.

**Uwaga:** Allegro (DataDome) może w headless pokazać Captchę → 0 sprzedawców. Wtedy trzeba VPS z VNC/`xvfb` albo odpalać lokalnie z oknem (`--headed`).

Nie commituj `browser_profile/` ani `data/*.csv`.

## Uwagi

- Nie wszyscy sprzedawcy mają publiczny telefon → `raw_note=brak_telefonu_w_publicznym_widoku`.
- Bot **nie omija** Captchy – czeka na ręczne przejście.
- Ustaw rozsądne `delay_*` i limity.

## Struktura

```
ledy_sprzedawcy/
  main.py
  config.example.yaml
  allegro_bot/
  data/          # sellers.csv / .sqlite po runie
  browser_profile/
```
