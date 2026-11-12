# SPL Registry

The Sydney Propulsion Lab registry: a Home Assistant add-on that runs inductions and checklists (preflights, QC checks, inspections) and keeps their records.

| Path | What it is |
| --- | --- |
| `PLAN.md` | The design: flow, data, security, phases, open decisions |
| `spl_registry/` | The add-on. `DOCS.md` there covers installing and using it |
| `repository.yaml` | Makes this folder a Home Assistant add-on repository (for when it moves to GitHub) |
| `docker-compose.yml` | Runs the add-on on a laptop for testing |

Inductions and checklists are written on the SPL wiki as pages tagged `record`. The wiki's splregistry plugin (`dokuwiki-plugin-spl-registry`) publishes them as `registry.json`, which the app downloads.

Records are personal data. They live only in the add-on's `/data` folder (and Home Assistant backups), never in this folder or in git.

## Testing

```
cd spl_registry
pip install -r requirements.txt pytest httpx
python -m pytest -q
```
