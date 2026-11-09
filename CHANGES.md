# Změny ve verzi 2.0 (oproti předané verzi 1.0)

Účel projektu zůstal stejný: deterministická synchronizace plánovacího XLSX z Google Drive do samostatných
Google kalendářů techniků. Bezpečnostní invarianty (žádní attendees, `sendUpdates=none`, cizí události se nikdy
nemění ani nemažou, mazání ve výchozím stavu vypnuto, first-run gate) jsou zachovány a pokryty testy.

## Nalezené nedostatky a jejich oprava

| # | Nedostatek ve verzi 1.0 | Oprava |
| - | --- | --- |
| 1 | `fcntl`/`fchmod`/`fchown` – kód nešel ani importovat na Windows | `calendar_sync/platform.py` (zámek a práva funkční na POSIX i Windows) |
| 2 | **Stale „dva odlišné snapshoty“ šlo obejít**: dvě synchronizace téhož souboru (ručně nebo `force_sync`) označily událost ke smazání | streak se počítá jen jednou na verzi Drive (`missing_fingerprint`); `force_sync` už stale nepotvrzuje |
| 3 | Každá úprava XLSX přepsala **všechny** události (verze/hash v privátních vlastnostech) – zbytečné API volání a „updated“ šum | volatilní `source_version`/`source_hash` se při porovnání ignorují |
| 4 | Jedna chyba Google u jednoho technika shodila celý běh | izolace po technicích, stav `partial`, chyba u technika v UI |
| 5 | Událost ručně smazaná v Google mezi listem a updatem → 404 shodí běh | update 404/410 událost znovu vytvoří; delete 404/410 je v pořádku |
| 6 | Mapování událost↔Google ID se ukládalo až na konci technika | commit hned po každém vytvoření (žádné osiřelé události při pádu) |
| 7 | Šablona názvu kalendáře (`calendar_template`) se ukládala, ale nepoužívala (platil prefix) | `calendar_name()` – šablona je autoritativní |
| 8 | UI nabízelo plán „pevné časy“, týdenní zálohy, SMTP, proxy hlavičky, locale, log level – nic z toho nefungovalo | UI nabízí jen funkční volby (interval i pevné časy, denní/týdenní záloha) |
| 9 | Každá drobná změna nastavení (i čas zálohy) zablokovala worker | blokuje jen změna zdroje a pravidel čtení XLSX (`MATERIAL_FIELDS`); zbytek se projeví hned |
| 10 | `stale_policy = disabled` se ignorovala | respektuje se |
| 11 | Selhání zálohy ukončilo tik plánovače a záloha se opakovala každých 30 s | chyba se zaloguje/auditu a další termín se posune |
| 12 | Aktivní worker bez `next_sync_at` by nikdy neběžel | plánovač si termín dopočítá |
| 13 | Přihlášení bez ochrany proti hádání hesla | omezení pokusů (6 / 5 min), bezpečnostní hlavičky + CSP, žádné inline skripty/styly |
| 14 | Modul-level `app = create_app()` (vedlejší efekty při importu) | továrna `create_app` (`uvicorn --factory`) |
| 15 | OAuth vždy přes ruční vložení adresy z `localhost:8081` | při `PUBLIC_BASE_URL` na localhost se Google vrací přímo na `/oauth/callback` |
| 16 | Dry-run/sync výsledek jako surový JSON v `<pre>` | historie běhů s detailem, souhrnem a problémy čtení XLSX |
| 17 | Chybějící retry Google API, MD5 načítalo celý soubor do paměti | `num_retries=4`, streamované MD5 |
| 18 | Chyba v parseru: pravdivostní hodnota XML elementu (`or []`) | `is not None` |
| 19 | SQLite bez `foreign_keys`/WAL/busy_timeout | nastaveno při připojení |

## Nové funkce

- Vlastní heslo správce: volba při instalaci (`deploy/install.sh`), změna v *Nastavení → Heslo správce* (hash scrypt v databázi, minimálně 12 znaků, po změně odhlášení všech) a `calendar-sync set-password`.
- Projekt přejmenován na **Calendar Sync** (balíček `calendar_sync`, příkaz `calendar-sync`, služba/cesty `calendar-sync`); výchozí barvy kategorií jsou TMB/CTN/VDF.
- Hromadný onboarding (`Onboardovat všechny čekající`), validace e-mailů, potvrzení před odesláním pozvánek.
- Stránky Historie, Detail běhu, Ke smazání, Audit; průvodce s kroky a ukládáním zdroje/skenu jmen.
- `calendar-sync serve`, `run-local.ps1`.

## Vědomě ponecháno / k rozhodnutí

- Stale potvrzení vyžaduje dvě *různé* verze souboru – smazání v XLSX se tedy projeví až po dvou editacích (dle původní specifikace).
- Aplikace sama žádné e-maily neodesílá; jedinou zprávou je jednorázová pozvánka ke sdílenému kalendáři, kterou při prvním sdílení rozešle Google. Nepoužívaný SMTP modul a další „mrtvá“ nastavení jsou odstraněny.
- Cílem OAuth zůstává Desktop klient a jediný centrální účet; Workspace/„In production“ viz README.
- Živé volání Google nebylo spuštěno ani testováno (chybí přístup); chování proti Google je ověřeno testy s atrapami Google (tests/fake_drive_calendar.py); následně běželo na testovacím serveru.
