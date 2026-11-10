# splinductions: SPL induction bundles from the wiki

DokuWiki plugin for the Sydney Propulsion Lab wiki. It builds the induction bundle that the
**SPL Inductions** Home Assistant add-on imports (the same `format: 1` JSON as
`tools/build_inductions.py` produced) from wiki pages tagged `induction`, and publishes it as a
media file the app downloads.

Requires the **tag**, **approve** and **sqlite** plugins (all part of the hassio-dokuwiki add-on).

## How it works

- **Which pages:** every page in `whs:inductions` (setting `namespace`) tagged `{{tag>induction}}`.
  The same tag on pages elsewhere (the FabLab training pages use it) is ignored. Also excluded: namespace templates (`c_template`,
  `i_template`; setting `ignore_pages`). New pages in `whs:inductions` start from `c_template`.
- **Which revision:** the last **approved** revision of each induction page. Drafts never reach the
  app. SWP and RA pages only supply the text attached to items: their approved revision is used when
  there is one, otherwise their current revision (shown as version `draft`). SWP approval never holds
  an induction back.
- **When:** right after a page is approved, and in DokuWiki's background task runner whenever an
  approval, a tagged page, a deletion or a move has changed anything. A rebuild that gives the
  same bundle publishes nothing.
- **Where:** `whs:inductions:inductions.json` (setting `media_id`), at
  `<wiki>/lib/exe/fetch.php?media=whs:inductions:inductions.json`. Every published bundle is kept
  as a media revision. Reading it needs read access to the namespace: give the app its own wiki
  account and use HTTP Basic auth or a DokuWiki API token.
- **Errors and warnings:** each induction is published on its own. If an approved induction page
  itself is broken (no Induction ID, no checklist items, a duplicate ID), only that induction keeps
  its last published version; every other induction still updates. A reference that doesn't match
  the SWP, a missing SWP page, or a Requires that isn't published yet is a warning: the item is
  published without the SWP text. Both show on the induction page and in `~~INDUCTIONSTATUS~~`.
- **Checks on save:** saving an induction page builds it as a draft against the approved SWPs and
  shows any errors and warnings to the editor straight away.

The JSON needs a MIME type. Add this line to `conf/mime.local.conf`:

    json  application/json

## Page format

    {{tag>induction}}
    <grid>
    <col xs="6">{{:whs:usyd_logo_dark.png?nolink&160|}}</col>
    <col xs="6"><wrapper class="h3">**Induction Checklist**</wrapper></col>
    </grid>

    ====== Creality CR-10 and CR-10 Max induction ======

    <datatables plain="true" striped="false" header-background="#eef2f5">

    ^ Induction ID:      | IND_J11_N209B_CR10 ||| **Version:** <badge type="success">@APPROVE_VERSION@</badge> |
    ^ Type:              | equipment ||| **Issued:** @APPROVE_DATE@ |
    ^ Location:          | J11 N209B ||||
    ^ SWP:               | [[whs:swp:creality-cr-10-printers]] ||||
    ^ RA:                | - ||||
    ^ Grants access:     | creality-cr-10: Creality CR-10, Creality CR-10 Max ||||
    ^ Requires:          | [[whs:inductions:ind-001]] ||||
    ^ Refresh (months):  | 12 ||||
    ^ Self-service:      | no ||||
    ^ Approved by:       | @APPROVE_NAME@ ||||

    </datatables>

    One or two sentences shown to the inductee at the top.

    ===== Hazards =====

      * [ ] Never touch the hot end or bed while printing ([[whs:swp:creality-cr-10-printers|hazard: Burns]])
      * [demo] Loads filament and starts a print from the SD card ([[whs:swp:creality-cr-10-printers|step 7]])
      * [ack] I will not use the CR-10 unsupervised until this induction is confirmed
      * Information only, not ticked

- **Header table:** laid out like the SWP header (logo, `datatables`, and the approve plugin's
  `@APPROVE_VERSION@`, `@APPROVE_DATE@` and `@APPROVE_NAME@`, which fill in once a revision is approved).
  The build reads the first value cell of each row, so the extra cells are ignored. The rows it uses:
  `Induction ID` (never change it once records exist), `Type` (`equipment` or
  `space`), `Location`, `SWP` and `RA` (links, or `-`), `Grants access` (comma separated, `Name`
  or `id: Name`; card readers use the ids), `Requires` (links to other induction pages, or
  induction ids), `Refresh (months)` (0 for no expiry) and `Self-service` (`yes`/`no`).
- **Items:** `[ ]` covered by the assessor, `[demo]` demonstrated by the inductee, `[ack]`
  acknowledged by the inductee; any other list item or paragraph is information only.
- **References:** a link to an SWP or RA page (in `whs:swp` or `whs:ra`, or named in the header)
  whose label starts with a keyword attaches that text to the item for the tablet:

  | Label | Matches |
  | --- | --- |
  | `intro` | the SWP's introduction paragraph |
  | `hazard: Burns` | the hazard whose name starts with "Burns" |
  | `step 7` | step 7 in the Steps section |
  | `section: Emergency shutdown` | the section whose heading starts with that |
  | `risk: Fire` | an RA risk heading in the Risks section |
  | `emergency: First aid` | a line in the RA's Emergency controls |

  A link with only an anchor, `[[whs:swp:x#emergency_shutdown]]`, means that section. If a
  reference stops matching the SWP, the item is published without its text and the page shows a warning.
  Step numbers shift when steps are added, so prefer hazards and sections.

## Settings

| Setting | Default |
| --- | --- |
| `tag` | `induction` |
| `namespace` | `whs:inductions` (tagged pages outside it are ignored; empty for the whole wiki) |
| `swp_namespaces` | `whs:swp` |
| `ra_namespaces` | `whs:ra` |
| `media_id` | `whs:inductions:inductions.json` |
| `require_approval` | on |
| `ignore_pages` | `(^\|:)(c_\|i_\|_)template$` |

## Testing

`php _test/offline.php <dir>=<namespace> ...` builds a bundle from page files without DokuWiki,
e.g. `php _test/offline.php swp-pages=whs:swp induction-pages=whs:inductions`.
