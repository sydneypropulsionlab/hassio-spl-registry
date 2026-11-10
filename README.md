# SPL Inductions

The induction system for the Sydney Propulsion Lab: a Home Assistant add-on that runs equipment and space inductions and keeps the induction register.

| Path | What it is |
| --- | --- |
| `PLAN.md` | The design: flow, data, security, phases, open decisions |
| `spl_inductions/` | The add-on. `DOCS.md` there covers installing and using it |
| `repository.yaml` | Makes this folder a Home Assistant add-on repository (for when it moves to GitHub) |
| `docker-compose.yml` | Runs the add-on on a laptop for testing |

Induction checklists are written in the SWP folder (`WHS\SWP\inductions\`) and built into `output\inductions.json`, which is uploaded to the app.

Records are personal data. They live only in the add-on's `/data` folder (and Home Assistant backups), never in this folder or in git.

## Testing

```
cd spl_inductions
pip install -r requirements.txt pytest httpx
python -m pytest -q
```
