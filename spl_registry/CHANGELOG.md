# Changelog

## 0.13.0

**Renamed to SPL Registry** (slug `spl_registry`). This is a new add-on as far as Home Assistant is concerned: install it fresh, then restore a backup from SPL Inductions on the setup page if you want the old records. The database file is now `registry.db`. The access API's internal address becomes `http://local-spl-registry:8099/api/v1`.

- **Registry bundle from the wiki.** The app reads `registry.json` (format 2, published by the wiki's splregistry plugin from pages tagged `record`), which holds inductions and checklists. The older `inductions.json` (format 1) still loads. Set `bundle_url` to `...?media=whs:inductions:registry.json`.
- **Checklists** (preflights, QC checks, inspections), on the new **Checklists** page: start one, fill it in on a phone or tablet, submit it.
  - Item kinds from the wiki page: tick, Pass / Fail / N/A, a reading with limits (outside them is a fail), a choice (a choice ending in `!` on the wiki is a fail), short text, formatted notes, and photos. Items can also ask for a photo (optional or required) and a comment (always, or required when the item fails).
  - Drafts save as you go (choosing a photo saves the draft); **Submit** checks every required answer and marks what's missing.
  - The outcome uses the page's own words from its `Outcome` row: two results such as `Go / No-go` or `Airworthy / Grounded` (the first when nothing failed, the second when anything failed), or one word such as `Complete`. Failed items are listed at the top of the record. The records filter is **Passed / Failed**, so it works across checklists with different words.
  - Photos are resized to 1600 px, keep the time they were taken and lose everything else in their metadata (including location), and are stored in the database so backups include them.
  - A submitted checklist can't be changed. Corrections are amendments with a reason, shown on the record and logged; the original answers stay.
  - Records list with filters by category, checklist and outcome, CSV export, and **Print / PDF** on each record. The dashboard shows recent checklists.
- **Categories are dynamic.** The Inductions and Checklists filters are built from the categories the wiki pages use, so a new category just appears.
- **One Registry page** replaces Inductions, Checklists and Register: start any induction or checklist from one list (filter by type and category), continue drafts, and see the records in two tabs (the induction register, with **Add a record** and the CSV exports, and checklist records). The old addresses redirect to it. The wiki sync and bundle upload moved to **More > Wiki and bundles**.
- **The wiki is checked whenever you open the site** (Dashboard, Registry, Wiki and bundles): a new version that only adds or changes things is loaded straight away and the page refreshes; one that would retire something is held and shown as an alert. **Check the wiki now** is renamed **Update Registry** and is also on the Registry page.
- **Signature required** is now set per record on the wiki page (`Signature required: yes`), replacing the add-on's `require_drawn_signature` option. Inductions: the inductee draws a signature as well as typing their name. Checklists: whoever submits it signs; the signature is shown on the record.


## 0.12.0

- **Induction bundles from the wiki.** Set `bundle_url` (and `bundle_username`/`bundle_password` or `bundle_token`) and the app downloads the bundle the wiki publishes from approved induction pages, every `bundle_poll_minutes` (default 10), importing it when it changes. The Inductions page shows the last check and has **Check the wiki now**.
- A bundle that would retire an active induction is held until an assessor clicks **Import and retire**, so removing a tag on the wiki can't silently retire an induction.
- **Dashboard is now an overview:** counts of current inductions, people inducted, completions in the last 30 days, what's waiting on someone, expired and expiring soon, then open sessions and those lists. Expired and expiring are separate lists, and an induction someone has since redone no longer shows as expired.
- **Starting an induction moved to the Inductions page:** the searchable list (by name, room, equipment, access or ID) with Preview and Start session, now also showing each induction's ID, refresh period, sources and checklist version.
- **Register is now Registry** in the menu and on its page (the address `/register` is unchanged).
- Upload still works as before. Wiki imports show as "Wiki" in the bundle history and are recorded in the audit log.

## 0.11.1

- **Open Web UI** (and the optional sidebar panel) now opens the app at your `base_url` instead of Home Assistant's address on port 8099. Turn on **Show in sidebar** on the add-on's Info page to add SPL Inductions to the Home Assistant sidebar.

## 0.11.0

- **UniKey** for everyone: asked on the join form, the manual record form and Edit details, and shown on the register, person pages, records, the sign-off page and both CSV exports (search the register by UniKey too). People are identified by UniKey; existing people get theirs filled in when they next join or are edited, and are marked "UniKey missing" until then. A UniKey and email that point to different people are refused rather than merged.
- **Access API** (`/api/v1`, read-only, keyed by UniKey) for the tap-card add-on: check one person against one resource, look up a person, list resources, and fetch everyone's current access in one call (with ETag so polling is cheap). See More > API keys and the API reference page.
- **API keys**: created and revoked on More > API keys, shown once, stored hashed, rate-limited, with last-used time and use count.
- Induction files: `grants_access` entries can have fixed ids (`- form4: Formlabs Form 4`), and `requires:` lists inductions that must also be current (e.g. the room's space induction).
- New option `access_grace_days` (default 0): extra days of access after an induction expires.

## 0.10.0

- Dashboard: **Start an induction** is now a list grouped by room, with a search box (name, room, equipment or what it grants access to) and All / Spaces / Equipment filters, so it stays usable with many inductions.

## 0.9.2

- Equipment inductions can also list what they grant access to (e.g. the machines covered), shown in the Grants access strip on the certificate.

## 0.9.1

- Certificate: the Grants access strip sits at the bottom of the page, just above the footer text.

## 0.9.0

- Certificate: removed the signature lines; "Room access required" is now a general **Grants access** list (each item shown separately), placed under the date boxes.
- Induction files use `grants_access:` (a list) instead of `room_access:`. The old name still works, and records signed under older bundles still show their access.
- The app and emails say "Grants access" throughout.

## 0.8.0

- **Compare with current**: when an induction has changed since a record was signed, the record shows a banner and a button to a git-style comparison: added items in green, removed in red, reworded items with the changed words highlighted, changed SWP excerpts side by side, changed source document versions and settings. A version picker compares against any earlier uploaded version, and "Show changes only" hides unchanged items.

## 0.7.0

- **PDF certificate** (A4 landscape, SPL branded): name, induction, location, induction date, how long it's valid, renewal due date, assessor, and the room access required for space inductions.
- Emailed automatically as soon as the inductee confirms, attached to the "Induction certificate" email, which replaces the old "Induction confirmed" email.
- New add-on option **certificate_cc**: a list of email addresses CC'd on every certificate email.
- Completed records have **Certificate** (view/download) and **Email certificate** (send again) buttons. Manual records get a certificate on request but nothing is emailed automatically.
- The Email log shows CC addresses and attachments.

## 0.6.1

- Inductions can list the rooms they're needed for (`room_access` in the induction file). Shown on the dashboard, the Inductions page, the checklist preview and each record.

## 0.6.0

- The first-run setup page can restore a backup instead of creating an account, bringing back every record and assessor account (with their passwords) on a new or reinstalled add-on.

## 0.5.2

- SPL icon and logo for the add-on in Home Assistant's Add-on Store and add-on page.

## 0.5.1

- Fixed link-style buttons (Download backup, Add a record, Sign off and others) losing their text when hovered.

## 0.5.0

- **More > Backup**: download the whole database as one `.db` file, safe to take while the app is in use.
- Restore from a backup file: the app checks it's a genuine, undamaged SPL Inductions database, shows it side by side with the current one, and asks you to type RESTORE.
- The current database is saved as a safety copy before every restore (newest 10 kept), and any safety copy can be downloaded or restored to undo.
- Downloads and restores are recorded in the audit log.

## 0.4.2

- The header stays at the top of the screen while scrolling.

## 0.4.1

- Stylesheet, script and logo links carry a version tag, so browsers and proxies load the new files straight after an update instead of a cached copy.

## 0.4.0

- SPL styling: white header with the full-colour SPL logo, SPL blue (#0064DE) for buttons, links and ticks, orange for items the inductee acknowledges, violet for demonstrations.
- SPL icon for the browser tab and phone home screen.
- Printed records keep the logo at the top.

## 0.3.0

- **Edit** a completed record: induction date, who did it, refresh period, notes, and the induction itself for manual entries. A reason is required and the before and after values show in the record's history. Expiry is recalculated.
- **Edit details** on a person's page to fix a name, email or role.
- **Delete** a record permanently with a reason; the audit log keeps a short note of what was deleted. People with no records can be deleted.
- Manual entries can still be cancelled instead of deleted.

## 0.2.0

- Add records by hand for inductions done outside the app, such as those on the old Google Form or paper: person, induction, date, who did it, refresh period and notes. **Register > Add a record**, or **Add a record** on a person's page.
- Manual records are marked as manual entries, show the induction date, skip the checklist and email confirmation, and can be cancelled to fix mistakes.
- The register shows each person's most recent induction by date, so back-dated records don't replace newer ones.
- Web addresses in checklist items are clickable.

## 0.1.0

First version.

- Assessor accounts (first account created on first visit; others added from the Assessors page).
- Upload the induction bundle (`output/inductions.json`) built from the SWP folder.
- QR code sessions: inductees scan, enter their name, email and role, and follow the checklist on their phone. Groups are supported.
- Assessor ticks covered items once for the group and demonstrations per person; inductees tick their own acknowledgements and sign by typing their name.
- Assessor sign-off with a refresh period chosen per record, then an email confirmation link (a button on the page, so link scanners cannot confirm).
- Records are locked once confirmed and keep a copy of the checklist and source document versions they were signed against.
- Register by person and induction, expiry warnings, flags when an SWP has changed since sign-off, CSV exports, email log and audit log.
