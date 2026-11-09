# Calendar Sync

> **Verze 2.0** – přepracované webové rozhraní (Jinja2 šablony, světlý/tmavý režim, samostatné stránky),
> běh na Windows i v Dockeru a řada oprav. Podrobnosti jsou v [CHANGES.md](CHANGES.md).

## Rychlý start

| Cíl | Příkaz |
| --- | --- |
| Běh na Windows proti skutečnému Google | `.\run-local.ps1` (vyžádá heslo, vytvoří `.local\`) |
| Docker (server) | viz „Spuštění Docker Compose / Portainer“ níže |
| Testy | `.venv\Scripts\python -m pytest -q` · `ruff check .` · `mypy calendar_sync` |

Webové rozhraní: **Přehled** (stav, poslední běhy), **Technici** (e-maily, onboarding), **Historie** (detail každého běhu,
dry-run), **Ke smazání** (stale události), **Audit**, **Nastavení**, **Průvodce** (první spuštění).
Aplikaci spouští `uvicorn calendar_sync.web:create_app --factory` (Docker) nebo `calendar-sync serve` (lokálně).

Produkční, deterministická služba **bez AI**, která čte plánovací XLSX z Google Drive a synchronizuje práci do samostatných Google kalendářů techniků.

## Architektura a bezpečnost

- FastAPI administrační web, SQLAlchemy + SQLite v `/data`, APScheduler (plán podle Nastavení: interval nebo pevné časy).
- Oficiální Google Drive/Calendar API a centrální OAuth účet, který vlastní technické kalendáře.
- OAuth client je Docker secret; refresh token se vytvoří do persistentního `/data/google-token.json` (režim 0600). Hodnoty se nelogují.
- Admin má jedno silné heslo (zvolené při instalaci, změnitelné v Nastavení, uložené jako hash), podepsané session cookies (`HttpOnly`, `SameSite=Strict`, v produkci `Secure`) a CSRF token ve všech měnících formulářích.
- Kontejner běží jako UID 10001, read-only root filesystem, bez capabilities, s `no-new-privileges`.
- Aplikace nikdy nenastavuje attendees a create/update/delete událostí vždy používá `sendUpdates='none'`. Jedinou zprávou je **Jednorázová pozvánka ke sdílenému kalendáři** při prvním vložení ACL `reader` (`sendNotifications=True`); nejde o pozvánku k události a opakovaný onboarding nic neposílá.
- Přihlášení je pouze jedním administračním heslem. E-mail technika slouží výhradně k udělení read-only ACL ke kalendáři a nikdy neposkytuje přístup do administrace.
- Událost je vlastněná pouze tehdy, má-li současně naše private properties `source_file_id`, `technician_name` a `source_key`. Cizí události se nemění ani nemažou.

Tok: first-run gate → naplánovaný čas → stability delay → metadata Drive → metadata + download + MD5 → metadata znovu → parser → plán create/update/stale → Calendar API → SQLite audit/status. Pokud se verze/MD5/velikost během běhu změní, běh je `deferred` a příští běh ho zopakuje. Dokud není wizard dokončen, worker synchronizaci ani zálohu nespustí.

## Povinný first-run wizard a Settings

Po prvním startu se přihlaste administračním heslem. Dashboard přesměruje na `/setup`; aktivace je možná až po validním nastavení, úspěšném testu Drive/Calendar a dry-run preview. Wizard i pozdější `/settings` ukládají změny auditovaně do SQLite:

- IANA časové pásmo (výchozí `Europe/Prague`);
- synchronizace buď intervalem 1–10080 minut, nebo seznamem lokálních časů `HH:MM`; UI ukazuje další běh a DST gap posune na první platnou minutu;
- stability/debounce 0–3600 sekund;
- automatický backup `disabled`/daily/weekly, lokální čas, retence podle počtu i stáří a relativní cíl vynuceně pod `/data/backups`;
- Drive file ID nebo běžná Drive URL, prefix kalendářů a základní stale/delete politika;
- automatické nalezení jmen techniků v plánovacím XLSX; admin jim přiřazuje e-mail přímo na dashboardu.

Heslo ke Google účtu se nikdy nezadává. JSON OAuth klienta lze vložit v Nastavení (uloží se do chráněného volume s právy jen pro službu) nebo dodat jako soubor/Docker secret; refresh token vzniká autorizací a leží v persistentním volume.

### Správa techniků

Jména se automaticky získávají z plánovacího XLSX na Google Drive. Admin na dashboardu doplní nalezenému jménu e-mail, případně jméno ignoruje nebo později obnoví. Technikův e-mail slouží pouze pro read-only sdílení jeho Google kalendáře a nezakládá přístup do administrace.

### Existing Solutions Preflight

- **n8n** je dobré pro jednoduché spojení Drive → Calendar, ale složitý stavový parser XLSX, přesné pokračovací řádky, source-key identity, dvousnapshotová stale politika a bezpečné vlastnictví událostí by skončily v rozsáhlém custom-code node a obtížně auditovatelném workflow.
- **Google Apps Script** je vhodný pro jednoduché Google Sheets, ale zdrojem je XLSX, nikoli nativní Sheet; konverze mění provozní model a kvóty/runtime i dlouhodobý stav stale synchronizace jsou horší pro tento případ.
- **Custom služba** je zde vhodná: parser je izolovaný a testovatelný, reconcile deterministický, SQLite drží audit a dva stabilní snapshoty, a nasazení má explicitní health/readiness/backup hranice.

## Parser a synchronizační pravidla

- pouze validní pětidenní listy `d.m. - d.m.YYYY`, jejichž hlavičkové datumy odpovídají názvu;
- konfigurovaný jmenný sloupec zahajuje blok technika; blok má 1..N řádků a prázdné jméno pokračuje až k dalšímu neprázdnému jménu nebo konci relevantní oblasti;
- jména se mapují přes canonical ID, workbook name a aliases; neznámé jméno je `unmapped_technician`, nevytvoří kalendář ani události;
- datumové hlavičky se auto-detekují v konfigurovaném rozsahu (nebo od explicitního prvního datového sloupce); sheet pattern a mapping lze otestovat přes `/setup/workbook-mapping` nad read-only snapshotem z Google Drive;
- layout fingerprint, chybějící hlavičky/sheet a výrazný propad assignmentů zastaví apply fail-safe bez mazání a zapíší audit alert;
- `summary` je celý obsah buňky (pouze CRLF/CR se normalizuje na LF);
- časová zóna `Europe/Prague`, explicitní interval v buňce má přednost, jinak celodenní událost;
- konzervativně rozpoznaný telefon jde do `description` jako `Kontakt: ...`; location vznikne jen při jediném jednoznačném vzoru adresy;
- barvy: TMB `3` magenta, CTN `9` modrá, VDF `11` červená;
- source identita je v private extended properties, bez attendees;
- v2 `source_key` nepoužívá číslo řádku: workbook profile + canonical technician + datum + job ID/no-ID content hash + occurrence fingerprint; reconcile umí bezpečně převést jednoznačnou legacy událost bez duplikace;
- chybějící vlastněná událost musí chybět ve **dvou po sobě jdoucích stabilních, odlišných Drive snapshotech**, teprve pak je `pending_stale`;
- mazání je ve výchozím stavu vypnuté (Nastavení → Mazání zmizelých událostí, vyžaduje potvrzení). Při zapnutí se smaže jen pending událost prokazatelně vlastněná touto službou. Doporučení: nejdřív několik cyklů provozovat s `false` a pending seznam auditovat.

## Google příprava a OAuth bootstrap

1. V Google Cloud projektu povolte Drive API a Calendar API.
2. Aplikace potřebuje OAuth client typu **Desktop app** v `secrets/google_oauth_client.json`.
   - Vytvořte Desktop OAuth client v Google Cloud a JSON buď vložte ve webovém rozhraní (Nastavení → Google účet), nebo ho nahrajte do `secrets/google_oauth_client.json` na Docker hostu. Nikdy ho nevkládejte do repozitáře ani do chatu.
3. Centrálnímu účtu zpřístupněte zdrojový XLSX alespoň pro čtení; file ID nebo URL zadáte až ve first-run wizardu.
4. Vygenerujte zbývající lokální secrety podle `secrets-example/README.md`.
5. Vytvořte volume a jednorázově spusťte OAuth bootstrap interaktivně na důvěryhodném stroji:

```sh
cd calendar-sync
cp .env.example .env
mkdir -p secrets
chmod 700 secrets
read -s -p "Administratorske heslo: " ADMINPASS
printf '%s' "$ADMINPASS" > secrets/admin_password.txt
unset ADMINPASS
echo
openssl rand -base64 48 > secrets/session_secret.txt
# Kontejner běží jako UID/GID 10001; Compose secrets jsou zde lokální bind mounty.
chown 10001:10001 secrets/admin_password.txt secrets/session_secret.txt secrets/google_oauth_client.json
chmod 400 secrets/admin_password.txt secrets/session_secret.txt secrets/google_oauth_client.json
docker compose build --pull
docker compose run --rm -p 8081:8081 calendar-sync calendar-sync oauth-bootstrap
```

CLI vypíše autorizační URL a dokončí podporovaný OAuth flow; token uloží do volume, nikoli do image/repozitáře. Nikdy token nekopírujte z pilotu a nevypisujte jej do logu/chatu.

## Google OAuth: Testing, Workspace a dlouhodobý provoz

> **Důležité:** Pro přístup k soukromému Google Drive a Kalendáři zde nejde primárně o API key. Aplikace používá OAuth 2.0: OAuth client JSON a dlouhodobě uložený **refresh token**. Týdenní obnova se týká refresh tokenu vydaného v režimu **Testing**, nikoli běžného access tokenu.

### Pilotní režim

- V Google Cloud Console otevřete projekt, ve kterém je OAuth klient pro `calendar-sync`.
- Povolte **Google Drive API** a **Google Calendar API**.
- V **Google Auth Platform → Branding** nastavte název a kontaktní údaje.
- V **Google Auth Platform → Audience** zkontrolujte testovací uživatele a **Publishing status**.
- V režimu **Testing** může Google vydaný refresh token zneplatnit přibližně po 7 dnech. Je proto nutná nová OAuth autorizace; samotný restart kontejneru ani nový access token tuto lhůtu neřeší.

### Produkční nasazení s Google Workspace

1. Přihlaste se účtem z požadované Google Workspace organizace.
2. Pokud aplikaci používají pouze účty stejné organizace, nastavte Audience jako **Internal**, pokud to typ a pravidla Workspace dovolují.
3. V **Google Auth Platform → Audience → Publishing status** zvolte **Publish app / In production**. Přepnutí do produkce není totéž co založení Workspace účtu; musí být provedeno samostatně.
4. Proveďte novou OAuth autorizaci produkčním Workspace účtem se scope pouze pro potřebný Drive a Calendar přístup.
5. Nový refresh token uložte do persistentního `/data/google-token.json` přes podporovaný bootstrap. Nekopírujte token z pilotu a nikdy ho nevkládejte do README, repozitáře, chatu ani logů.
6. Ověřte `GET /readyz`, `calendar-sync status`, dry-run a následně běžný sync.

**Odpověď na otázku:** Ano, pokud bude OAuth aplikace skutečně v režimu **In production**, pravidelné sedmidenní obnovování refresh tokenu kvůli režimu Testing nebude potřeba. Samotný Google Workspace účet to ale automaticky nezaručuje. I u Workspace účtu je nutné zkontrolovat Publishing status a udělat novou autorizaci. Refresh token může být později zneplatněn z jiných důvodů — například ručním odebráním přístupu, změnou hesla nebo překročením limitu tokenů — proto musí zůstat bezpečně uložený a `/readyz` se má monitorovat.

Oficiální dokumentace: [Google OAuth 2.0](https://developers.google.com/identity/protocols/oauth2), [OAuth consent screen](https://support.google.com/cloud/answer/10311615).

## Instalace na čistý Linux server bez Dockeru (Debian/Ubuntu, LXC, VM)

```sh
git clone https://github.com/<vas-ucet>/<repozitar>.git && cd <repozitar>
sudo ./deploy/install.sh http://IP-SERVERU:8080      # veřejná adresa aplikace
```

Skript nainstaluje Python, vytvoří uživatele `calsync`, virtuální prostředí, systemd službu `calendar-sync`, náhodný klíč pro cookies a **zeptá se na heslo správce, které si sami zvolíte** (min. 12 znaků; pro automatizaci `sudo ADMIN_PASSWORD='…' ./deploy/install.sh …`).
V repozitáři nejsou žádné tajné údaje, jména ani hesla. Pak otevřete aplikaci, přihlaste se zvoleným heslem
a postupujte podle stránky **Nápověda** (OAuth klient vložíte přímo v aplikaci). Aktualizace: `git pull && sudo ./deploy/install.sh` (heslo se nemění). Heslo lze později změnit v *Nastavení → Heslo správce*, případně ztracené heslo obnovit příkazem `calendar-sync set-password` na serveru.

## Spuštění Docker Compose / Portainer

```sh
docker compose config
docker compose build --pull
docker compose run --rm calendar-sync calendar-sync init-db
docker compose up -d
docker compose ps
docker compose exec calendar-sync calendar-sync status
```

V Portaineru vytvořte Stack z `compose.yaml`, nastavte `.env` hodnoty, nahrajte tři secret soubory na host do adresáře stacku a zachovejte named volume `calendar_sync_data`. Port `8080` publikujte jen do LAN nebo za HTTPS reverse proxy. Při čistém HTTP pro jednorázový LAN test lze nastavit `COOKIE_SECURE=false`; v produkci je to nevhodné.

Po přihlášení dokončete povinný wizard, načtěte jména techniků z plánovacího XLSX, přiřaďte jim e-maily na dashboardu, spusťte test připojení a zkontrolujte dry-run. Teprve **Aktivovat setup** odblokuje worker. Read-only parsování ukládá unikátní nalezená jména do SQLite; na dashboardu je lze přidat nebo trvale ignorovat a později obnovit. **Onboard** vytvoří nebo znovu použije `<nastavený prefix><Jméno>`, idempotentně nastaví ACL `reader`, uloží `calendarId` a zobrazí subscribe link. Pouze první vložení ACL odešle **Jednorázovou pozvánku ke sdílenému kalendáři**; nejde o pozvánku k události. E-mail technika není přihlašovacím údajem do aplikace. Na iPhonu může být ještě nutné kalendář povolit na <https://www.google.com/calendar/syncselect>, poté v iOS vypnout/zapnout synchronizaci Kalendářů a kalendář v aplikaci zaškrtnout.

Při aktualizaci existujícího nasazení stačí před spuštěním služby provést bezpečnou aditivní inicializaci; vytvoří pouze chybějící tabulku a sloupce:

```sh
docker compose run --rm calendar-sync calendar-sync init-db
docker compose up -d
```

## Provoz

- UI: `http(s)://host:8080/` – přehled, technici, onboarding, dry-run, sync-now, historie běhů, stale, audit, nastavení.
- `GET /healthz`: proces žije. `GET /readyz`: DB, config a OAuth token. `GET /metrics`: Prometheus metriky.
- CLI:

```sh
docker compose exec calendar-sync calendar-sync dry-run
docker compose exec calendar-sync calendar-sync sync-now
docker compose exec calendar-sync calendar-sync status
docker compose logs -f --tail=200 calendar-sync
```

Logy jsou strukturované JSON a nemají obsah tokenu. Scheduler má jedinou instanci a coalescing; nespouštějte více web workerů, protože embedded scheduler je určen pro jeden proces.

### Backup / restore

Aplikace sama zálohuje SQLite databázi podle plánu v Nastavení (bez OAuth tokenu). Kompletní offline záloha včetně tokenu je nejbezpečnější při krátkém zastavení služby:

```sh
docker compose stop calendar-sync
docker run --rm -v calendar-sync_calendar_sync_data:/data -v "$PWD/backup:/backup" alpine \
  sh -c 'cd /data && tar czf /backup/calendar-sync-data-$(date +%F-%H%M%S).tgz calendar-sync.db google-token.json'
docker compose start calendar-sync
```

Backup obsahuje OAuth refresh token: šifrujte jej, omezte přístup a retenci. Restore: službu zastavte, ověřte archiv, obnovte oba soubory do volume, nastavte vlastnictví UID/GID 10001 a režim tokenu 0600, spusťte službu a zkontrolujte `/readyz` a dry-run.

### Incident runbook

1. `/healthz` selže: `docker compose ps` a logy; ověřte read-only FS a write přístup UID 10001 do volume.
2. `/readyz` hlásí token: obnovte token z bezpečné zálohy nebo zopakujte OAuth bootstrap; nerecyklujte token z jiné aplikace.
3. Drive download/MD5: neaplikujte změny, zkontrolujte file ID a počkejte na další stabilní poll.
4. Calendar 401/403: ověřte OAuth scopes, vlastníka kalendáře a ACL; žádné ruční rozšiřování scopes bez revize.
5. Pending stale: ponechte `DELETE_ENABLED=false`, proveďte dry-run a porovnejte dva stabilní zdrojové snapshoty. Mazání zapněte jen po provozním schválení a se zálohou.
6. Obnova: restore DB+tokenu, dry-run, až potom sync-now.

## Vývoj a ověření

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
mypy calendar_sync
```

Testy nemají síťové side effects; používají fake Google backend. Image sestavení ověřte příkazy výše na hostu s Dockerem.
