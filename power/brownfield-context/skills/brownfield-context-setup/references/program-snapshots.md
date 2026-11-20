# Per-program configuration snapshots

Many systems keep per-program (per-deployment, per-tenant) configuration in database tables. `bcp variants` and `generate` (with `program_snapshots`) import read-only exports of that table and join them with the code: which constant reads each key, where it is read, its code default, and which programs a change to a key affects.

The tool never connects to a database. Someone with read access exports the table to a file. Export only the configuration table (key and value columns), never business data.

## CSV format

- Encoding UTF-8. A byte-order mark is tolerated. Line endings LF or CRLF.
- One header line, exactly `program_id,property,property_value` (or `program_id,key,value`).
- One row per key: `program_id` (short program name), `property` (the key exactly as stored), `property_value` (the value exactly as stored).
- Quoting follows RFC 4180: a value with a comma, a double quote or a line break is wrapped in double quotes, inner quotes doubled. Example: `"1:1, 2:2"` is the value `1:1, 2:2`.
- A file may hold one program or several (one `program_id` per row).
- An empty value (nothing after the last comma) means the row exists and its value is the empty string.
- A key with no row is *absent*. Outputs show "absent: code default applies" with the default found in the code.
- A key may appear only once per program. A wrong header or a duplicate key stops the run with an input error (exit 2).

Example:

```csv
program_id,property,property_value
north,feature.mode,strict
north,feature.mapping,"1:1, 2:2"
north,feature.label,
```

## JSON format

Either one object per program:

```json
{"program_id": "north", "synthetic": false, "properties": {"feature.mode": "strict", "feature.label": ""}}
```

or a list of rows: `[{"program_id": "north", "property": "feature.mode", "property_value": "strict"}]`. Numbers and booleans are read as their JSON text.

## Synthetic data

A file whose name starts with `SYNTHETIC-`, or a JSON object with `"synthetic": true`, marks its programs SYNTHETIC. Every output labels them, and `program-config.md` opens with a banner saying the data is invented. Use this for demos and tests, so nobody mistakes the values for a real deployment.

## Masking

`variant_mask_patterns` (regexes matched against the key; default: password, passwd, secret, token, credential, private, apikey, api_key) replaces matching values with `***masked***` at import, before anything is compared or written. Values longer than 200 characters are clipped. Add patterns for any key that may hold personal data or credentials.

## Commands

```
python "<scripts>/bcp_cli.py" variants --bre "<bre>" --repo "<repo>" --snapshots "<file-or-folder>" --out "<out>" [--key <key-or-constant> ...]
```

It writes `<out>/program-config.md` and `.json`: the programs, a comparison of the keys whose effective value differs between programs or from the code default, "programs a change to K affects" for each `--key` (or every differing key), and the key catalogue (constants, code default, read sites with domains, extraction rules naming the key, values compared near the read sites).

To put the map into the Kiro context, add `"program_snapshots": ["<folder>"]` to `bcp.config.json` and run `generate`. The `impact-analysis` specialist and the `change-impact-analysis` skill then use `.kiro/context/program-config.md`.
