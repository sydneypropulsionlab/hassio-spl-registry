# SPL Induction System: Plan

A self-hosted app for inducting people on equipment and into spaces in the Sydney Propulsion Lab (J11), keeping an auditable record of every induction. It replaces the Google Form, runs as a Home Assistant add-on on the University Home Assistant, and takes its content from the SWPs and risk assessments in `WHS\SWP`.

Status: version 0.11.0 of the add-on is built and tested (section 8). Git and OneDrive syncing are deferred for now.

---

## 1. Goals

- **Equipment inductions:** a custom checklist for each machine, with content drawn from that machine's SWP.
- **Space inductions:** a checklist for each room (exits, eyewash, first aid, spill kits, room rules), drawn from the room's risk assessment.
- **Assessed in person:** an assessor walks through the checklist with the inductee, both sign, and the inductee confirms by email.
- **A real register:** who is inducted on what, by whom, against which SWP version, and when it expires.
- **Custom refresh periods:** a default for each induction, adjustable per person at sign-off.
- **Records that hold up:** locked once confirmed, with every change logged, and exportable to PDF and CSV.

Assessors: Dries Verstraete, Ben, Brock Cooper.

---

## 2. How it fits together

```
WHS\SWP\                                  University Home Assistant
  swps\*.md      ─┐                         ┌──────────────────────────────┐
  ras\*.md       ─┼─ build ─► inductions    │  SPL Inductions add-on       │
  inductions\*.md─┘           bundle (.json)┼─►  import bundle             │
                              validated     │   run inductions (tablet)    │
                                            │   records, register, PDFs    │
                                            │   email confirmations        │
                                            └──────────────────────────────┘
```

There are two halves:

1. **Authoring, in the existing SWP folder.** Induction checklists are Markdown files in a new `WHS\SWP\inductions\` folder, written and built the same way as the SWPs. The build checks every reference to the SWP or RA still exists and produces an **induction bundle**: one JSON file holding every induction, the SWP and RA versions it was built from, and a content hash.
2. **The app, on the University Home Assistant.** It runs inductions from the bundle and keeps the records. It never reads the OneDrive folder directly; you upload a new bundle from the admin page when the inductions change.

The app is decoupled from OneDrive, and every record can show exactly which version of the checklist and SWP it was done against.

---

## 3. Induction files

The checklist is custom, not a copy of the SWP. Items are written for the walkthrough, and an item can point at the SWP or RA content it covers. The build attaches that text so the assessor can expand it during the walkthrough. If the SWP changes and the referenced hazard or step disappears, the build fails, so the checklist can't drift silently.

### Equipment induction example

```markdown
---
id: IND_J11_S120_FORM4
title: Formlabs Form 4 induction
type: equipment
location: J11 S120
swp: SWP_J11_S120_Formlabs_Form_4
refresh_months: 12
self_service: false
---

## Before you start

- [ ] Has read SWP J11 S120 Form 4 {swp: intro}
- [ ] Knows where the Safety Data Sheets are kept for the resins and isopropyl alcohol

## Hazards

- [ ] Resin is a skin and eye irritant and a sensitiser; why glove discipline matters {swp: hazard "Skin sensitisation"}
- [ ] Isopropyl alcohol is highly flammable; wash unit lid closed, no ignition sources {swp: hazard "Fire and explosion risk"}
- [ ] Never use a metal tool in the resin tank {swp: hazard "Damage to the resin tank film"}

## Demonstrated by the inductee

- [demo] Puts on and removes nitrile gloves without contaminating hands
- [demo] Removes the build platform and carries unwashed parts on a covered tray {swp: step 11}
- [demo] Runs a wash and cure cycle {swp: step 14}

## Emergency

- [ ] Can show the eyewash station, spill kit and extinguisher {ra: emergency "Eyewash"}
- [ ] Knows how to stop a job and isolate the printer {swp: section "Emergency shutdown"}
```

### Space induction example

```markdown
---
id: IND_J11_S120_SPACE
title: J11 S120 space induction
type: space
location: J11 S120
ra: RA_J11_S120_3D_Printing
refresh_months: 24
self_service: false
---

## Getting around

- [ ] Room access, opening hours and who to contact
- [ ] Emergency exits and the J11 assembly area {ra: emergency "Building fire warden"}

## Safety equipment

- [ ] First aid kit and first aid officer {ra: emergency "First aid kit"}
- [ ] Eyewash station and safety shower {ra: emergency "Eyewash"}
- [ ] Spill kits and extinguishers {ra: emergency "Spill kits"}

## Room rules

- [ ] No food or drink near the printers
- [ ] Which equipment needs a separate induction before use
```

### Item types

| Marker | Meaning | Who ticks it |
| --- | --- | --- |
| `- [ ]` | Covered and explained | Assessor, with the inductee |
| `- [demo]` | Inductee has demonstrated it | Assessor only |
| `- [ack]` | Inductee acknowledges a rule | Inductee |
| plain text | Information only, not ticked | — |

### Reference syntax

- `{swp: hazard "<start of hazard name>"}`
- `{swp: step <n>}`
- `{swp: section "<heading>"}`
- `{ra: emergency "<start of line>"}`
- `{ra: risk "<task name>"}`

Text is matched by how it starts, so references survive small wording changes.

---

## 4. The app

### Induction flow: QR session (main flow)

1. **Start.** The assessor logs in on the lab tablet and taps **Start session** on an induction. The tablet shows a QR code and a six-character code. Joining stays open for 30 minutes (configurable) and can be reopened.
2. **Join.** Each inductee scans the QR code with their phone camera, or types the code at the app's address, and enters their full name, email and role. Groups are fine: everyone in the session joins on their own phone. Rejoining with the same email returns to the same checklist.
3. **Walk through together.** The assessor ticks each **Assessor covers** item once for the whole group, and ticks each person's **Inductee demonstrates** items in the participants list as they do them. Each inductee ticks their own **I acknowledge** items on their phone. Ticks appear on the inductee's phone within a few seconds. SWP and RA text can be expanded beside each item.
4. **Inductee signs.** When every item is done, the inductee ticks the declaration and types their name exactly as entered (a drawn signature can be required in the add-on options). The tablet shows them as **Ready for sign-off**.
5. **Assessor signs off.** The assessor picks the refresh period (default from the induction file, adjustable per person, or no expiry), adds any notes, ticks their declaration and signs. The record becomes **awaiting confirmation** and the inductee is emailed a confirmation link, valid for 7 days (configurable).
6. **Inductee confirms.** The link opens a page with a **Confirm** button. Confirming is a button press, not just opening the link, so University email link scanners (Safe Links) can't confirm on someone's behalf. The record becomes **complete**, is locked, and a copy is emailed.

Assessors can resend the email or cancel an unconfirmed record. Cancelled records stay in the log.

### Single-device fallback

If an inductee has no phone, they join on the tablet itself: scan or open the join link on the tablet, enter their details, tick their acknowledgements and sign there, then the assessor returns to the session page and signs off. No separate mode is needed.

### Self-service link (later, per induction)

For inductions marked `self_service: true`, typically low-risk space orientation or pre-reading, an assessor sends a link. The inductee works through the checklist and declaration on their own, then an assessor reviews and countersigns before it counts. Equipment inductions stay in person, because the SWPs require competence to be demonstrated. Not built in 0.1.0.

### Register and renewals

- **Register:** people against inductions, with status (current, expiring within 30 days, expired, unconfirmed), filterable by room, equipment and person.
- **Person view:** every induction a person holds, with links to each printable record.
- **SWP change flag:** when a new bundle changes an SWP's version or content hash, current inductions against it are flagged "SWP updated since induction". Assessors decide whether re-induction is needed.
- **Expiry:** the dashboard lists records expired or expiring within 30 days (configurable). Reminder emails are planned, not built in 0.1.0.
- **Export:** CSV of the register and of all records. Each record page prints cleanly to PDF from the browser.

### Records

A record stores:

- the person
- the induction ID
- a snapshot of the exact checklist used, with its bundle hash and the SWP or RA version it was built from
- each ticked item with who ticked it
- typed names and signatures
- the assessor
- the refresh period and expiry date
- timestamps for start, sign-off, email sent and confirmation
- notes

**Corrections are logged.** Assessors can edit a completed record's date, assessor, refresh period and notes (and the induction, for manual entries) with a required reason; the old and new values are kept in the record's history. The checklist and signatures of an app induction can't be changed. Deleting a record needs a reason and leaves a short note in the audit log. Every action goes into an append-only audit log: who did it, what changed and when.

---

## 5. Technical design

| Part | Choice | Why |
| --- | --- | --- |
| Backend | Python, FastAPI | Can reuse the Markdown parsing already written for the SWP tools |
| Front end | Server-rendered pages (Jinja) with a small vanilla JavaScript file for ticks and live updates (polling every 3 s) | Works on a tablet and phones, no build step, no external scripts |
| Database | SQLite in `/data` | One file, included in Home Assistant backups |
| PDFs | Browser print of the record page for now; Typst PDFs later | Keeps the image small; print styles hide the navigation |
| Email | SMTP, settings in the add-on options | University relay |
| Signatures | Typed name, optional drawn signature (canvas, stored as PNG) | Typed name plus email confirmation is the core proof |
| QR codes | `qrcode` library, SVG | Generated in the app, nothing sent to outside services |

### Data model

- **people:** id, name, email (unique), role, created
- **assessors:** id, name, email, password hash, active. Dries, Ben and Brock to start.
- **bundles:** id, uploaded by, uploaded at, hash, JSON
- **inductions:** id, version, title, type, location, default refresh months, self-service flag, active, checklist JSON (including source SWP or RA versions and hashes), bundle id
- **sessions:** id, join code, induction version, assessor, join-until time, closed time, the group's covered-item ticks
- **records:** id, session, person, induction version, assessor, status (joined, inductee signed, awaiting confirmation, complete, cancelled, superseded), private participant link token, covered/demo/acknowledgement ticks with who and when, inductee typed name and signature, refresh months, expiry, notes, checklist snapshot, confirmation token hash and expiry, timestamps, supersedes
- **outbox:** every email, with whether it was sent (so the app works before SMTP is set up)
- **audit_log:** time, actor, action, record or entity, details

### Access and security

- **Assessor side:** email and password login (scrypt hashes, at least 10 characters), signed session cookie, CSRF tokens on every form and tick, login rate limiting. Assessors add each other and hand over a one-time temporary password.
- **Public side:** the join page (needs a live session code, rate-limited), each inductee's checklist (a private random link), and the confirmation page (single-use token, stored hashed, expires). Nothing else is reachable without logging in.
- **Reachability:** inductees' phones must reach the app. On University Wi-Fi this depends on whether phones can reach the Home Assistant host's address (see decisions). Confirmation links need the same reachability from wherever people read their email.
- **Encryption:** 0.1.0 serves plain HTTP on port 8099. Put it behind HTTPS (a University reverse proxy or the Home Assistant NGINX add-on) before go-live if it's reachable beyond the lab network.

### Home Assistant add-on

- **Contents:** an add-on repository (`repository.yaml`) holding the `spl_inductions` add-on: `config.yaml`, `Dockerfile` (python:3.13-slim, uvicorn), `DOCS.md`, `CHANGELOG.md`, the app and its tests.
- **Options:** base URL, time zone, SMTP host, port, user, password, sender and STARTTLS, join window minutes, confirmation link days, expiry warning days, and whether drawn signatures are required.
- **Storage:** everything lives in `/data` (`inductions.db`, `secret.key`), so Home Assistant backups include it.
- **Ports:** port 8099 on the Home Assistant host serves both assessors and inductees. Ingress isn't used, because inductees don't have Home Assistant accounts.
- **Install:** as a local add-on (copy the folder into `/addons`) to start; a GitHub add-on repository later.
- **Outside Home Assistant:** the same image runs with `docker compose` for development or a later move.

### Changes to the SWP folder

- A new `inductions\` folder with an `_TEMPLATE.md` starter.
- `tools\build_inductions.py` resolves references, validates them and writes `output\inductions.json`.
- `build.ps1` and `build.sh` gain an inductions step.
- The README and CLAUDE.md gain a section on induction files.

---

## 6. Phases

1. **Induction files and build.** Done: format, reference resolver, validation, JSON bundle, `_TEMPLATE.md`, build-script step. First files: Form 4 and the S120 space induction (drafts to review). Still to write: J5 PolyJet, CR-10 printers, Windshaper, N107 space.
2. **App core.** Done in 0.1.0: assessor accounts, bundle upload, QR sessions with groups, inductee checklist and signing on their phone, assessor sign-off, email confirmation, register, person view, records, CSV exports, email log, audit log, SWP change flags, optional drawn signatures.
3. **Pilot.** Install on the University Home Assistant, set the base URL and SMTP, check phones on University Wi-Fi can reach it, run one real Form 4 induction.
4. **Records and renewals.** Typst PDF records, reminder emails before expiry, supersede-on-refresher, backup and restore test.
5. **Go-live.** Enter past Google Form responses as manual records with their original dates (built in 0.2.0), switch over, retire the form.
6. **Later, if wanted.** Self-service links, QR codes on equipment showing who is inducted, Home Assistant entities (e.g. expiring inductions) for dashboards or notifications, GitHub versioning.

---

## 7. Tap-card access (agreed design)

- A **separate add-on** links card IDs to UniKeys and runs the readers. This add-on only answers "who is inducted on what", keyed by UniKey.
- **UniKey** is required for everyone and is the person's identity. No student or staff numbers.
- **Resources** are the `grants_access` entries of each induction, with stable ids (`- form4: Formlabs Form 4`, or an id made from the name). `requires:` lists inductions that must also be current, e.g. equipment requires its room's space induction.
- **Rule:** allowed when a completed, unexpired, non-cancelled record grants the resource and every required induction is current. No grace period (`access_grace_days` = 0). The SWP-updated flag doesn't block.
- **API:** read-only, API keys per system, the card add-on calls `http://local-spl-inductions:8099/api/v1` on Home Assistant's internal network and can cache `GET /api/v1/access` (ETag) so readers keep working if this add-on restarts.

## 8. Decisions still needed

- [ ] **Network:** can phones on University Wi-Fi (eduroam or UniSyd) reach the Home Assistant host on port 8099? If not: a University-hosted reverse proxy, or join only on the lab tablet.
- [ ] **Link reachability:** do confirmation links need to work off campus?
- [ ] **SMTP:** University relay details, or another mail service. Until set, emails are logged on the Email log page and links can be handed over manually.
- [ ] **Identifying people:** email alone, or also UniKey or student and staff number.
- [ ] **Signatures:** is a typed name plus email confirmation enough, or should drawn signatures be required (an add-on option)?
- [ ] **Retention:** how long records are kept, and what happens when someone leaves.
- [ ] **Google Form history:** import it, or archive it as-is?
- [ ] **Ownership:** who writes and approves induction files (suggest the same review as SWPs).
- [ ] **Ben:** full name and email, for his assessor account.

---

## 9. Build status (0.11.0)

| Part | State |
| --- | --- |
| `WHS\SWP\inductions\*.md`, `tools\build_inductions.py` | Working; full builds write `output\inductions.json` |
| Add-on app (`Inductions\spl_inductions\app`) | Working; end-to-end tests pass (`python -m pytest -q`) |
| Tested in a browser | Assessor on a 1280 px tablet view, two inductees on phone views, group session through to confirmation and register |
| Docker image | `requirements.txt` installs cleanly and the app runs exactly as the container command; the image itself hasn't been built on Home Assistant yet |
| Manual records (0.2.0) | Assessors can add one past record at a time (person, induction, date, assessor, refresh period), for transferring Google Form history |
| Editing and deleting (0.3.0) | Edit completed records and people's details with a logged reason; delete records with a reason |
| Backup and restore (0.5.0) | Download the database, restore from a file with checks and a side-by-side comparison, automatic safety copy before each restore |
| Certificates (0.7.0) | Branded A4 PDF certificate emailed on confirmation, CC'd to the `certificate_cc` list; download and resend from the record |
| Compare view (0.8.0) | Git-style diff of the checklist a record was signed against versus the current or any earlier version |
| UniKey and access API (0.11.0) | UniKey identity on every person; read-only `/api/v1` keyed by UniKey (check, person, resources, full access list with ETag); API keys page; resource ids and `requires` in induction files; `access_grace_days` option |
| Not built yet | Reminder emails, Typst PDFs, superseding records, self-service links, bulk CSV import |
