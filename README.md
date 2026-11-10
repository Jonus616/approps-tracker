# Appropriations Tracker (mockup)

Staff-review mockup for Concord Action. Not an official Concord page; not indexed.

- `index.html`: a copy of the live concordaction.org/appropriations-tracker-2027 page (Nov 10) with only the chart image
  replaced by the tracker. `widget.html`: the tracker block alone, the part that would go into WordPress.
- Data: `python -I scripts/fetch_status.py --out data/approps.json` (Congress.gov bill records, House Clerk and
  Senate roll calls; no AI in the data path). Bill list: `config/bills.json`.
- Daily refresh: `.github/workflows/refresh.yml`, schedule switched off.
