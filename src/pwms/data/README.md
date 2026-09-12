# Bundled place data

Reference data for the country/city pickers, loaded into `Country` / `City` with:

```bash
manage.py load_places
```

| File | Rows | Contents |
| --- | --- | --- |
| `countries.csv` | 252 | `code` (ISO 3166-1 alpha-2), `iso3`, `name`, `continent` |
| `cities.csv.gz` | 47,360 | `name`, `ascii_name`, `country_code`, `latitude`, `longitude`, `population` |

## Source and licence

Both files are extracts of the [GeoNames](https://www.geonames.org/) export
(`countryInfo.txt`, `cities15000.zip`, `ZA.zip`), which is licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) — **that attribution
has to stay visible** wherever the data is published.

The city list is `cities15000` (every city over 15,000 people, plus capitals)
merged with **every** South African populated place from `ZA.zip`, so small
South African towns and township sections are findable too; `cities15000` alone
carries only 304 of them. Rows are de-duplicated on
`(name, country_code, latitude, longitude)`.

## Regenerating

```bash
curl -O https://download.geonames.org/export/dump/cities15000.zip
curl -O https://download.geonames.org/export/dump/ZA.zip
curl -O https://download.geonames.org/export/dump/countryInfo.txt
```

Then rebuild `countries.csv` from `countryInfo.txt` (columns 1, 2, 5, 9) and
`cities.csv.gz` from the tab-separated dump files (columns `name`, `asciiname`,
`latitude`, `longitude`, `country code`, `population`, keeping `feature class
P` for `ZA.txt`).
