# logsift

Mine a scheduled job's own log files into a metric series, and refuse the number that is really a timestamp.

A nightly job writes one log file per run. Somebody wants a chart of how many records it
updates, so a short script greps each file for a label and a number and loads the pairs into a
table. It works for a week. Then the charts flat-line at zero and stay there for a month, and
nobody notices, because the charts are not empty. They are confidently wrong.

The line that did it:

```
2026-08-11 03:14:07 - INFO - Duration: 0:00:07
```

The matcher reads `INFO - Duration` as the metric name and mines `info_duration = 0.0` from the
leading zero of the clock. The real duration, 7.13, is mined from the same line. Both collapse
onto one canonical key, the zero arrives second, and the series says the job now finishes
instantly. `Start time: 2026-08-11 03:14:07` mines a metric of 2026 the same way.

The same class of bug bit twice more while this tool was being rebuilt. `Duration: 0:00:07
Elapsed time: 0:01:00` mines seven seconds from the clock matcher, then mines `elapsed_time = 0`
from the label matcher, which reads the `0` of the second clock. The two raw labels differ, so a
guard that deduplicates on the raw label lets both through, and `elapsed_time` canonicalizes onto
`duration_seconds`. Zero wins again. The fix is to deduplicate on the canonical key, and one
assertion holds that line.

```
$ python logsift.py --self-test
logsift self-test: no network, no database, no arcpy
--------------------------------------------------------------------
PASS  a thousands separator is read as one number
...
PASS  '2026-08-11 03:14:07 - INFO -' mines exactly one pair, seven seconds  <-- pinned defect
PASS  '2026-08-11 03:14:07 - INFO -' never mines an info_ key  <-- pinned defect
PASS  '2026-08-11 03:14:07 | INFO |' mines exactly one pair, seven seconds  <-- pinned defect
PASS  '[2026-08-11 03:14:07]' mines exactly one pair, seven seconds  <-- pinned defect
PASS  a logger's module field does not become part of the key
PASS  stripping the preamble does not swallow the payload  <-- pinned defect
PASS  a start time really does mine the year as a number  <-- pinned defect
PASS  and start_time is refused as a metric  <-- pinned defect
PASS  a second duration on one line cannot overwrite the first with a zero  <-- pinned defect
PASS  and the raw pass really does mine that zero, so the guard is load bearing  <-- pinned defect
PASS  because elapsed_time and duration are one series  <-- pinned defect
PASS  the clock matcher reserves its own label, so 'duration' is not also mined as zero  <-- pinned defect
PASS  a hand formatted clock with a one digit seconds field is still a clock, not the zero of its hours  <-- pinned defect
...
PASS  a real zero is mined: only a zero that is a clock is refused
PASS  and no supplied rule can invent a metric from it  <-- pinned defect
PASS  a metric really named error_count is not cut down to count  <-- pinned defect
PASS  a supplied rule beats a built-in one  <-- pinned defect
PASS  a rule written against the canonical key reaches the label too  <-- pinned defect
PASS  the csv is LF only, so Windows and Linux write the same bytes  <-- pinned defect
PASS  Logs, logs, Log and log resolve to one candidate  <-- pinned defect
PASS  an ancestor working directory is refused  <-- pinned defect
PASS  so one shared log is not charted as every job's own  <-- pinned defect
PASS  an ancestor spelled with a .. is refused too  <-- pinned defect
PASS  an ancestor is refused even when the script's folder is not local  <-- pinned defect
PASS  Logs and LOGS are ONE directory where the filesystem folds case  <-- pinned defect
...
PASS  the real duration survives the whole pipeline  <-- pinned defect
PASS  and the start time never enters the file
PASS  and it is not counted as a log file that was read  <-- pinned defect
PASS  --apply is OFF by default  <-- pinned defect
PASS  one bad path among good ones fails the whole run  <-- pinned defect
PASS  stdout carries ONLY the data, so a redirect gives a clean csv  <-- pinned defect
PASS  --out without --apply writes NOTHING  <-- pinned defect
PASS  and writes it INSTEAD of stdout, not as well as  <-- pinned defect
PASS  and writes exactly what the dry run printed  <-- pinned defect
PASS  and leaves the existing file untouched  <-- pinned defect
PASS  a write that fails is exit 2, not a false success
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  the self-test leaves no temporary directory behind
--------------------------------------------------------------------
196 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Nothing to install, no `arcpy`, no third-party package, no network and no
database. It runs on ArcGIS Pro's Python and on a plain `python3` equally. The self-test above
runs 196 assertions on Windows and the same 196 on Linux.

```
git clone https://github.com/uhsear/logsift.git
```

## Quick start

```
python logsift.py --self-test
python logsift.py C:\jobs\parcels\Logs
```

## Usage

Point it at a folder of logs, at one log file, or at the job's script.

```
python logsift.py parcels/Logs
python logsift.py parcels/Logs/nightly_20260917.log
python logsift.py --script parcels/nightly.py
```

```
$ python logsift.py parcels/Logs
logsift: 3 log file(s), 27 line(s), 12 metric row(s), 0 error line(s)
metric_key            metric_value  observed_at_utc            log_file
--------------------  ------------  -------------------------  --------------------
checked_record_count  47285         2026-09-15T07:15:00+00:00  nightly_20260915.log
checked_record_count  47285         2026-09-16T07:15:00+00:00  nightly_20260916.log
checked_record_count  47285         2026-09-17T07:15:00+00:00  nightly_20260917.log
duration_seconds      7.133761      2026-09-15T07:15:00+00:00  nightly_20260915.log
duration_seconds      7.133762      2026-09-16T07:15:00+00:00  nightly_20260916.log
duration_seconds      7.133763      2026-09-17T07:15:00+00:00  nightly_20260917.log
source_record_count   47285         2026-09-15T07:15:00+00:00  nightly_20260915.log
source_record_count   47285         2026-09-16T07:15:00+00:00  nightly_20260916.log
source_record_count   47285         2026-09-17T07:15:00+00:00  nightly_20260917.log
updated_record_count  1415          2026-09-15T07:15:00+00:00  nightly_20260915.log
updated_record_count  1416          2026-09-16T07:15:00+00:00  nightly_20260916.log
updated_record_count  1417          2026-09-17T07:15:00+00:00  nightly_20260917.log
```

Those logs also carry a start time, a batch size and a line reading
`Verification: 15,562 features`. None of the three is in the table. The first two are refused,
and the third needs a rule, because `verification` is not a word this tool charts on its own.

The data goes to stdout and every message goes to stderr, so a redirect gives a clean file:

```
$ python logsift.py parcels/Logs --format csv > metrics.csv
$ head -2 metrics.csv
metric_key,display_name,metric_value,observed_at_utc,log_file,sample_line
checked_record_count,Checked Record Count,47285,2026-09-15T07:15:00+00:00,nightly_20260915.log,"2026-09-15 03:14:03 - INFO - Records checked: 47,285"
```

Writing the file yourself needs `--apply`. Without it nothing is written:

```
$ python logsift.py parcels/Logs --format csv --out metrics.csv
would write 1838 byte(s) to metrics.csv. Nothing written: add --apply.
$ python logsift.py parcels/Logs --format csv --out metrics.csv --apply
wrote 1838 byte(s) to metrics.csv
```

| Flag | Default | What it does |
|---|---|---|
| `PATH` | none | Log files, or directories of them. A path that does not exist is a usage error. |
| `--script` | none | A job's script. Its own folder, and any `Logs` folder beside it, are searched. |
| `--working-directory` | none | The scheduled action's working directory, when it differs from the script's folder. |
| `--days` | `30` | Only log files modified in the last N days. |
| `--max-files` | `40` | At most N log files, newest first. |
| `--max-lines` | `5000` | At most N lines per file. `0` reads the whole file. |
| `--rules` | none | A JSON file of your own label rules. Checked before the built-in ones. |
| `--format` | `table` | `table`, `csv` or `json`. |
| `--out` | none | Write the rendering to this file instead of stdout. Needs `--apply`. |
| `--apply` | off | Write the `--out` file. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

A file you name on the command line is read whatever its age. The day window applies to what
`--script` and a directory find for you.

## What it mines

Three shapes of line, in this order, and at most one value per canonical key per line:

- **A duration written as a clock.** `Duration: 0:00:07`, `Total time: 1:02:30`. Converted to
  seconds. Read first, so the clock is never read as the number zero.
- **A label and a number.** `Records checked: 47,285`, `Total execution time: 970.13 seconds`.
- **A verb and a number.** `Updated 1432 records`, `Deleted 0 features`.

The label is then normalised to a key and mapped onto a canonical name, so that `Records
updated`, `Updated 1432 rows` and `updated_count` are one series rather than three. Eight
built-in rules cover the generic synonyms: duration, updated, deleted, inserted, checked,
processed, exported and source. Anything specific to your own jobs goes in a `--rules` file.

Each log file is one run, and the file's modification time is the observation time. One row per
file per metric, with the line the number came from, so a wrong number stays traceable.

## What it refuses

- **The log level.** The preamble is stripped once, before anything is matched. `INFO`,
  `WARNING` and a timestamp never become part of a metric name. There is no later rule that
  strips a level word off a key, because that rule turns a real `error_count` metric into a
  series called `count`.
- **A number that is a clock.** `start_time`, `time` and `finished` are dropped by name. A
  duration written as a clock is converted rather than read digit by digit.
- **A second value for a key already mined from that line.** The first value wins. This is the
  guard the flat-line defect needed, and deduplicating on the raw label is not enough for it.
- **A setting the job echoed back.** Any key ending in `batch_size` is configuration, not a
  measurement.
- **A hex build or commit id**, and `cpp` and `s_features`, which fall out of `arcpy` messages
  and traceback text.
- **A label with no measuring word in it.** `some_random_label: 5` and `Widgets: 12` are
  dropped. Give them a rule if you want them.
- **A shared working directory.** A scheduled action may declare a working directory that is an
  ancestor of the script's own folder. Measured on one host, 7 of 25 tasks declared the shared
  repository root. Accepting it mines one root-level log into every one of those jobs and reports
  one job's record count as all of them.

## Rules file

A JSON object. Each key is a regular expression matched against a label; each value is the
canonical key to use, or `null` to drop that label.

```json
{
  "^verification$": "verified_record_count",
  "^checked_record_count$": null
}
```

```
$ python logsift.py parcels/Logs/nightly_20260917.log --rules rules.json
logsift: 1 log file(s), 9 line(s), 4 metric row(s), 0 error line(s)
metric_key             metric_value  observed_at_utc            log_file
---------------------  ------------  -------------------------  --------------------
duration_seconds       7.133763      2026-09-17T07:15:00+00:00  nightly_20260917.log
source_record_count    47285         2026-09-17T07:15:00+00:00  nightly_20260917.log
updated_record_count   1417          2026-09-17T07:15:00+00:00  nightly_20260917.log
verified_record_count  15562         2026-09-17T07:15:00+00:00  nightly_20260917.log
```

Your rule is tried against the label as the log spells it, and again against the canonical key a
built-in rule produced. Both `^records_checked$` and `^checked_record_count$` reach that series,
so a rule written against the name you can see in the output does what it looks like it does.

A rule you supply is taken at its word. It skips the noise list and the vocabulary filter,
because you named the key yourself. That is how `verification` above gets mined at all.

## Exit codes

0 metrics mined, 1 nothing to chart, 2 the write step failed, 64 usage error.

Exit 1 is the interesting one. It means the tool read log files and no number in them survived
the rules, or found no log file inside the day window. Both are the failure this tool exists to
make visible, so neither is reported as success.

## Why not Promtail, or grep

Promtail's metrics stage, Datadog's log-based metrics and Splunk all do this properly, with a
query language, storage, alerting and a history behind them. If you already run one, use it.

They want an agent on the host and a server to ship to. On a Windows box where six scheduled
scripts each write into their own `Logs` folder, there is no agent, and nobody is going to
install one to answer "is the nightly job still updating the same number of records". This tool
is the 30 second answer for that box: point it at the folder, get a CSV, chart it in a
spreadsheet.

`grep` and `awk` will also get you label-and-number pairs, in one line each, and that is exactly
the tool that produced the flat zero. The part that is hard is not the extraction. It is
refusing the numbers that are not measurements, and noticing that two spellings are one series.

## Limits

- It has no history and no state. Every run reads the log files that are there now. The series
  is as long as your retention, and rotation deletes your chart.
- A metric's value is the last one in the file, because a job that reports a running total
  reports the final one last. A job that reports the same key for two different things gets one
  row and the second value.
- A sentence count takes the words after the number as its key, so `Updated 1432 records in the
  parcels layer` charts as `updated_records_in_the_parcels_layer`. Write the label form, or give
  that phrasing a rule.
- Only `.log` and `.txt` are read from a directory. A `.csv` or `.json` beside the logs is
  somebody's data, and mining it would invent metrics out of column values.
- The observation time is the file's modification time, not a timestamp parsed out of the lines.
  A file appended to all day carries the time of its last write. One file per run is the shape
  this fits.
- No alerting and no thresholds. It does not know that 1417 is a reasonable number or that zero
  is not. It only refuses to mine a number that was never a measurement.
- `--out` overwrites an existing file when you pass `--apply`. Without `--apply` nothing is
  written, and the dry run says whether the file is already there.
- The vocabulary filter is a word list. A job that counts "widgets" needs a rule, and there is no
  way for the tool to guess that one for you.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [taskpulse](https://github.com/uhsear/taskpulse) - find the scheduled tasks whose logs these are, and which of them are silently failing
- [jobharness](https://github.com/uhsear/jobharness) - give the job the logging this mines, with retry and resume
