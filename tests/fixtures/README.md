# kaos-tabular test fixtures — provenance

Per `docs/oss/50-data-and-fixtures/provenance-policy.md`, every fixture
directory documents the source URL, license, retrieval date, and SHA-256
of every tracked file.

This directory holds 11 tabular fixtures in five formats (CSV, JSON,
XLSX, SQLite) used to drive the kaos-tabular engine, MCP, and CLI test
suites. The fixtures fall into three families:

1. **LEDES98B sample invoices** (`ledes98b.{csv,json,xlsx,sqlite}`) —
   four representations of the same legal-billing dataset, sourced from
   the open LEDES 1998B specification. Drives the realistic billing
   battle tests under `tests/unit/test_battle.py` and SQLite reader
   tests under `tests/unit/test_sqlite_register.py`.
2. **US state encyclopedia** (`states.{csv,json,xlsx,sqlite}`) — four
   representations of a 50-state reference dataset (name, capital,
   admission date, population, lat/lon, motto, description) used to
   exercise unicode handling, long-text columns, and 4-way cross-format
   joins.
3. **Hand-crafted micro-fixtures** (`simple.csv`, `records.json`,
   `unicode.csv`) — small synthetic tables created in-house for the
   express purpose of exercising specific reader behaviors (basic
   parsing, JSON-records ingestion, multi-script Unicode).

## Per-file manifest

| File | Source | License | Retrieved | SHA-256 |
|---|---|---|---|---|
| `ledes98b.csv` | LEDES 1998B specification sample, vendored from `kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.csv` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10). Upstream format spec published by the LEDES Oversight Committee at https://ledes.org/ledes-format-specifications/ | public (LEDES 1998B format specification example); 273V vendored representation | 2026-04-03 (first KAOS commit `aef1072`) | `b0cf21a199a090019cd487387ab510ce71c4b2a66026eb2ad873b51f846df781` |
| `ledes98b.json` | Re-serialization of `ledes98b.csv` to JSON-records by 273V (generated for kaos-tabular tests; hash differs from the kelvin upstream JSON) | public (LEDES 1998B format specification example); 273V derived representation | 2026-04-03 (first KAOS commit `aef1072`) | `ebed6449b82b9c9c7a057a7e17bf8fe780ce1adbc5d111baf9968015359d64e8` |
| `ledes98b.xlsx` | Vendored from `kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.xlsx` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10); same LEDES 1998B sample data as `ledes98b.csv` | public (LEDES 1998B format specification example); 273V vendored representation | 2026-04-03 (first KAOS commit `e03b9b5`) | `700e4f8029d74a526a8b849cd070ba4e08c7c7e00b9994c50319e344199f6770` |
| `ledes98b.sqlite` | Vendored from `kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.sqlite` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10); same LEDES 1998B sample data as `ledes98b.csv` | public (LEDES 1998B format specification example); 273V vendored representation | 2026-04-03 (first KAOS commit `e03b9b5`) | `056f9e7ea8a0cb991e23faa4ec3cd04b3c53d9c6e4a611824fa1c941eeb02b40` |
| `states.csv` | Vendored from `kelvin-modules/kelvin_tabular/samples/states/states.csv` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10). Field-level data (capital, admission date, population, lat/lon, motto, descriptive summary) is compiled from public-domain US federal sources (census.gov, state.gov) and per-state Wikipedia lead-paragraph excerpts (`https://en.wikipedia.org/wiki/<State_name>`) | CC-BY-SA-4.0 for Wikipedia-derived prose excerpts in the `description` and `motto` columns; public domain (17 USC §105) for the underlying federal facts. **See [`LICENSE-FIXTURES.md`](LICENSE-FIXTURES.md) for the full CC-BY-SA-4.0 attribution + share-alike notice.** | 2026-04-03 (first KAOS commit `aef1072`) | `7b1a2c024dbc457e213e8b42b74a81b8109fbefb3fefeb8a4f1fca71e3827e71` |
| `states.json` | Re-serialization of `states.csv` to JSON-records by 273V (generated for kaos-tabular tests; hash differs from the kelvin upstream JSON) | CC-BY-SA-4.0 / public domain (mixed, same as `states.csv`); 273V derived representation. **See [`LICENSE-FIXTURES.md`](LICENSE-FIXTURES.md).** | 2026-04-03 (first KAOS commit `aef1072`) | `121624ecbf36d83bdbf905e2a379a9350018b856aaef418ff02e2599f6726be2` |
| `states.xlsx` | Vendored from `kelvin-modules/kelvin_tabular/samples/states/states.xlsx` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10); same data as `states.csv` | CC-BY-SA-4.0 / public domain (mixed, same as `states.csv`); 273V vendored representation. **See [`LICENSE-FIXTURES.md`](LICENSE-FIXTURES.md).** | 2026-04-03 (first KAOS commit `e03b9b5`) | `3a4748fb80ebf1e381cfbfe979368132025ddf4823a3f68a73b9485c1f6edcbc` |
| `states.sqlite` | Vendored from `kelvin-modules/kelvin_tabular/samples/states/states.sqlite` (initial kelvin tabular commit `e8b7e1d`, 2025-05-10); same data as `states.csv` | CC-BY-SA-4.0 / public domain (mixed, same as `states.csv`); 273V vendored representation. **See [`LICENSE-FIXTURES.md`](LICENSE-FIXTURES.md).** | 2026-04-03 (first KAOS commit `e03b9b5`) | `7d7166006612736e9ae12c89cf05f78932bf70b33c819a44725505245ab55dc3` |
| `simple.csv` | hand-crafted, 273V — 10-row synthetic table (id, name, amount, date, active) used by `tests/conftest.py::simple_csv` and most CLI tests | proprietary, 273 Ventures | 2026-04-03 (first KAOS commit `aef1072`) | `4b090f8e38ea1529dffd2ea7e7cda9e52d40e5c502fc189ff8b261acde874c51` |
| `records.json` | hand-crafted, 273V — 5-row JSON-records mirror of the first five rows of `simple.csv` used by `tests/conftest.py::records_json` | proprietary, 273 Ventures | 2026-04-03 (first KAOS commit `aef1072`) | `c7c2afa3fcd539685ff1302b2f88ced07c1f78f386b9c7758f2cb448010e1553` |
| `unicode.csv` | hand-crafted, 273V — 5-row multi-script Unicode table (Latin, Japanese, German, Spanish, Chinese) used by `tests/conftest.py::unicode_csv` and unicode round-trip tests | proprietary, 273 Ventures | 2026-04-03 (first KAOS commit `aef1072`) | `40702bc65dd695163dae7d92b1b3522f2257d2149b8465ec9e80c6384d9f6096` |

The LEDES98B sample data originates from the publicly published
LEDES 1998B billing format specification examples — see the
specification text retained alongside the upstream copy at
`kelvin-modules/kelvin_tabular/samples/ledes/ledes98bi.txt`. The
specification itself is maintained by the
[LEDES Oversight Committee](https://ledes.org/) as an open billing
format; the sample invoices distributed with the specification are
made public for the purpose of vendor implementation and conformance
testing. The `states.*` family contains a mix of public-domain US
federal facts and Wikipedia prose excerpts; the CC-BY-SA-4.0 share-alike
obligation propagates to derivatives of those prose columns and is
documented in the dedicated [`LICENSE-FIXTURES.md`](LICENSE-FIXTURES.md)
attribution block (per-row License column above is the short summary
pointing back at that file).

## Refresh procedure

LEDES family:

```bash
# Refresh from the kelvin-modules upstream (in-tree historical source)
cp ../../../kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.csv tests/fixtures/
cp ../../../kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.xlsx tests/fixtures/
cp ../../../kelvin-modules/kelvin_tabular/samples/ledes/ledes98b.sqlite tests/fixtures/
# The .json copy in this directory was regenerated by 273V; re-derive
# it from ledes98b.csv if the upstream changes.
sha256sum tests/fixtures/ledes98b.{csv,json,xlsx,sqlite}
```

States family:

```bash
cp ../../../kelvin-modules/kelvin_tabular/samples/states/states.csv tests/fixtures/
cp ../../../kelvin-modules/kelvin_tabular/samples/states/states.xlsx tests/fixtures/
cp ../../../kelvin-modules/kelvin_tabular/samples/states/states.sqlite tests/fixtures/
# The .json copy in this directory was regenerated by 273V; re-derive
# it from states.csv if the upstream changes.
sha256sum tests/fixtures/states.{csv,json,xlsx,sqlite}
```

Hand-crafted (`simple.csv`, `records.json`, `unicode.csv`):

1. Edit the file via a normal PR.
2. Re-run `sha256sum tests/fixtures/<file>` and update the matching
   row in this README in the same commit.

After any refresh, update the matching row(s) in the manifest above and
re-run `pytest tests/ -v` to confirm the fixture still drives the
relevant tests cleanly.

## Confirmations (per provenance policy §"Backfill PR template")

- No file in this directory is customer / privileged / pseudonymized
  content. The LEDES sample data ships with the public LEDES 1998B
  specification for the express purpose of vendor implementation
  testing; the states data is a mix of public-domain US federal facts
  and Wikipedia prose excerpts; the three micro-fixtures are hand-crafted
  by 273V for kaos-tabular regression.
- The dep-license policy's denylist does not apply. The CC-BY-SA-4.0
  attribution obligation on the `states.*` prose columns is satisfied
  by this README and propagates to any derivative published outside
  this repo per the policy's case-by-case guidance.
