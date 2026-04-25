# gecitemre

## TUREB guide scraper

`scrape_tureb.py` walks https://www.tureb.org.tr/RehberVeritabani and
saves every guide to `guides.json` and `guides.csv`.

```bash
pip install -r requirements.txt
python scrape_tureb.py            # writes guides.json / guides.csv
python scrape_tureb.py --debug    # also dumps debug.html for the first page
python scrape_tureb.py --out data/tureb.json
```

The scraper auto-detects the form's hidden ASP.NET fields
(`__VIEWSTATE`, `__EVENTVALIDATION`, anti-forgery tokens) and the
results table, then walks the pagination — both `?page=N` query strings
and `__doPostBack(...)` style links are supported. Column names from
the live table become the keys in the JSON / CSV output.

If TUREB changes the markup and the scraper can no longer find the
table, rerun with `--debug` and inspect `debug.html` to update
`find_results_table` / `parse_headers`.

### About Me
a computer enthusiast
