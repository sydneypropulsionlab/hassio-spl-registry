# SPL Inductions

Run equipment and space inductions on a tablet, with inductees following along and signing on their own phones, and keep the induction registry.

## Install (local add-on)

1. Copy the `spl_inductions` folder into the `/addons` folder on the Home Assistant host, for example with the **Samba share** or **Advanced SSH & Web Terminal** add-on. You should end up with `/addons/spl_inductions/config.yaml`.
2. In Home Assistant go to **Settings > Add-ons > Add-on Store**, open the **⋮** menu and choose **Check for updates**. **SPL Inductions** appears under **Local add-ons**.
3. Open it and click **Install**. The first install builds the image on the host, which takes a few minutes.
4. On the **Configuration** tab, set **base_url** (see below), then **Start** the add-on.
5. Open `http://<home-assistant-address>:8099` and create the first assessor account. Whoever visits first becomes an assessor, so do this straight after starting it. If you're moving or reinstalling, use **Or restore from a backup** on that page instead: upload a `.db` backup from the app and every record and assessor account comes back, so you log in with your usual details.

To update, replace the folder in `/addons` and click **Rebuild** on the add-on page. Your data is kept.

## Opening the app

The add-on's **Open Web UI** button opens the app at your `base_url`. Turn on **Show in sidebar** on the add-on's Info page for a sidebar link that does the same. The app itself is served at `base_url` (e.g. through Nginx Proxy Manager), not inside Home Assistant.

## Configuration

| Option | What it does |
| --- | --- |
| `base_url` | The address phones use to reach the app, e.g. `http://10.65.1.20:8099`. It goes into the QR code and the email links, so it must work from a phone on the network inductees use. |
| `timezone` | Time zone for dates on pages, emails and exports. |
| `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `smtp_from`, `smtp_starttls` | Outgoing email. Port 465 uses SSL; other ports use STARTTLS when `smtp_starttls` is on. Leave `smtp_host` empty to run without email: messages are kept on the **Email log** page instead. |
| `session_minutes` | How long a session's join code works (5 to 240). Assessors can reopen joining. |
| `confirm_days` | How long the email confirmation link stays valid. |
| `expiry_warning_days` | How far ahead the dashboard counts and lists inductions as expiring soon. |
| `require_drawn_signature` | Make inductees draw a signature as well as typing their name. |
| `access_grace_days` | Extra days the access API keeps allowing someone after their induction expires. 0 (the default) means access ends on the expiry date. |
| `bundle_url` | Where to download the induction bundle from the wiki, e.g. `https://wiki.sydneypropulsionlab.org/lib/exe/fetch.php?media=whs:inductions:inductions.json`. Leave empty to load bundles by upload only. |
| `bundle_username`, `bundle_password` | A wiki account that can read the bundle (sent as HTTP Basic auth). Give it read access to `whs:inductions` only. |
| `bundle_token` | Instead of a username and password: a DokuWiki API token for that account (sent as a Bearer token). |
| `bundle_poll_minutes` | How often to check the wiki for a new bundle (default 10). 0 checks only when an assessor clicks **Check the wiki now**. |
| `certificate_cc` | Email addresses CC'd on every certificate email, e.g. a WHS inbox or supervisors. Add one per line; leave empty for none. |

## Loading inductions

### From the wiki (recommended)

Induction checklists live on the SPL wiki as pages tagged `induction` (normally `whs:inductions:ind-001`, `ind-002`, …). The wiki's **splinductions** plugin builds the bundle from the **approved** revision of every tagged page and publishes it as `whs:inductions:inductions.json`. Set `bundle_url` and a login above and the app picks up each new bundle by itself.

- **Edit, then approve.** Saving an induction page checks it straight away and shows any problem, such as a reference that no longer matches the SWP. Nothing reaches the app until the page is approved.
- **Errors never reach the tablet.** If an approved change can't be built, the wiki keeps publishing the last good bundle and lists the problem on the induction page and on `whs:inductions:start`.
- **Retiring needs a person.** When a new bundle drops an induction (for example its tag was removed), the app holds it and shows **Import and retire** on the Inductions page instead of retiring it on its own.
- **Check the wiki now** on the Inductions page fetches straight away.

### By upload

1. In the SWP folder, run a full build (`./build.ps1` or `./build.sh` with no file names). It writes `output/inductions.json` and fails if any induction refers to SWP or RA content that no longer exists.
2. In the app, go to **Inductions** and upload that file.

Uploading replaces the active set. An induction left out of the bundle is retired. Existing records keep the exact checklist they were signed against, and if an SWP or RA has changed since a record was signed off, the record and registry show **SWP updated since induction**.

## Running an induction

The **Dashboard** is an overview: counts of current inductions and people, what's waiting on someone (sign-off or email confirmation), and what has expired or expires soon, plus any open sessions.

1. **Inductions > Start session** on the induction (search by name, room, equipment or access). The tablet shows a QR code and a six-character code.
2. Each inductee scans the code with their phone camera (or opens the address and types the code) and enters their name, email and role. Several people can join one session.
3. Go through the checklist:
   - Tick **Assessor covers** items as you explain them. One tick covers the whole group.
   - Tick each person's **Inductee demonstrates** items in the participants list as they show you.
   - Inductees tick their own **I acknowledge** items on their phone.
4. When everything is ticked, each inductee signs on their phone and shows as **Ready for sign-off**.
5. Click **Sign off**, choose the refresh period, add notes, tick the declaration and sign. The inductee is emailed a confirmation link.
6. The inductee opens the link and presses **Confirm**. The record is complete, and they're emailed their PDF certificate (CC'd to the `certificate_cc` addresses). A completed record's page can show the certificate or email it again.

No phone? Let the inductee join and sign on the tablet, then sign them off.

If email isn't set up yet, open **Email log**, copy the confirmation link from the message and give it to the inductee.

## Records and the registry

- **Registry:** everyone against every induction, filterable by room and type, with CSV export.
- **Person page:** every record for one person.
- **Record page:** the full checklist with who ticked each item and when, signatures, source document versions and history. Print it to PDF from the browser.
- **Audit log:** every login, upload, session, tick-off, sign-off, confirmation, cancellation and export.

### What's changed since a record

If an induction's checklist or its SWP has changed since someone was signed off, their record shows a banner with **Compare with current**. It shows, git-style, what's different between the checklist they did and the current one: items added (green), removed (red) and reworded (changed words highlighted), any quoted SWP text that changed, and source document versions. Pick another version from the list to compare against an earlier upload. Use it to decide whether a refresher is needed.

### Correcting records

- **Edit** (on a completed record): change the induction date, who did it, the refresh period and notes, and for manual entries the induction. A reason is required, and the old and new values and the reason appear in the record's history. The checklist and signatures of an induction done in the app can't be changed.
- **Edit details** (on a person's page): fix a name, email or role. This applies to all their records.
- **Delete this record** (at the bottom of a record): removes it permanently, with a reason. The audit log keeps a short note of what was deleted, by whom and why, but not the checklist or signature. A person with no records left can then be deleted from their page.
- **Cancel** stops an unfinished induction (or withdraws a manual entry) but keeps it in the history.

## Adding past records

For inductions done before the app, such as responses on the old Google Form, use **Registry > Add a record** (or **Add a record** on a person's page). Enter the person, the induction, the date it was done and who did it; pick **Someone else** for an assessor without an account. The refresh period defaults to the induction's and the expiry is worked out from the induction date, so an old record may go straight to Expired.

Manual records are complete straight away, have no checklist or email confirmation, and are labelled as manual entries on the record, the person page and the CSV export. If you make a mistake, edit or delete it (see Correcting records).

## UniKeys

Everyone gives their UniKey when they join, and assessors enter it on manual records. It's how the app knows two records are the same person, and what the access API is keyed on. People recorded before UniKeys were added show **UniKey missing** on the registry; add theirs under **Edit details** (or it's filled in next time they join with the same email). If a UniKey is already recorded with a different email, the app refuses rather than merging; check and fix it under Edit details.

## Access API (for the tap-card add-on)

A read-only JSON API at `/api/v1` answers "may this UniKey use this resource?". Create a key under **More > API keys** (shown once; revoke it there), and see **API reference** on that page for the endpoints, responses and the internal address another add-on uses. Resource ids come from `grants_access` in the induction files; someone needs a current induction granting it plus every induction it `requires`.

## Backup and restore

**More > Backup** (any assessor):

- **Download backup** saves the whole database (people, records and signatures, checklists, assessors, email and audit logs) as `spl-inductions-<date>.db`. It is safe to do while the app is in use.
- **Restore from a backup**: upload a `.db` file from this page. The app checks it's a genuine SPL Inductions database, shows its contents next to the current database, and only restores once you type RESTORE. Everything is replaced, including assessor accounts and passwords; if your own account isn't in the backup you're logged out.
- Before every restore the current database is saved as a **safety copy** (the newest 10 are kept, inside the add-on). Download one or restore it to undo a restore.

The backup file holds personal information and assessor logins, so keep it somewhere access-controlled. Home Assistant backups of the add-on remain the full safety net (they also include the settings and session key).

## Assessors

**More > Assessors** adds an assessor and shows a temporary password once. Give it to them in person; they change it under **My account**. Deactivated assessors can't log in, and their past records are unchanged.

## Network and security

- Phones must be able to reach `base_url`. Check this on University Wi-Fi before the first induction. If phones can't reach the Home Assistant host, ask for a University reverse proxy, or have inductees join on the lab tablet.
- The add-on serves plain HTTP. If it's reachable beyond the lab network, put it behind HTTPS.
- Data (`inductions.db` and the session key) lives in the add-on's `/data` folder and is included in Home Assistant backups. It contains personal information: restrict who can download backups.
