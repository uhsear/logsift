# logsift

Mine a scheduled job's own log files into a metric series, and refuse the number that is really a timestamp.

A nightly job writes one log file per run. Somebody wants a chart of how many records it
updates, so a short script greps each file for a label and a number and loads the pairs into a
table. It works for a week. Then the charts flat-line at zero and stay there for a month, and
nobody notices, because the charts are not empty. They are confidently wrong.

The line that did it:

```
2026-03-04 03:14:07 - INFO - Duration: 0:00:07
```

The matcher reads `INFO - Duration` as the metric name and mines `info_duration = 0.0` from the
leading zero of the clock. The real duration, 7.13, is mined from the same line. Both collapse
onto one canonical key, the zero arrives second, and the series says the job now finishes
instantly. `Start time: 2026-03-04 03:14:07` mines a metric of 2026 the same way.

The same class of bug bit twice more while this tool was being rebuilt. `Duration: 0:00:07
Elapsed time: 0:01:00` mines seven seconds from the clock matcher, then mines `elapsed_time = 0`
from the label matcher, which reads the `0` of the second clock. The two raw labels differ, so a
guard that deduplicates on the raw label lets both through, and `elapsed_time` canonicalizes onto
`duration_seconds`. Zero wins again. The fix is to deduplicate on the canonical key, and one
assertion holds that line.

The same month had a second, quieter failure. The scripts moved to Windows PowerShell 5.1 and
began to write their logs with `>` and `>>`. Those write UTF-16LE with a byte-order mark, so the
file has a zero byte between every letter. Read as plain text, it holds no label and no number,
and the series stops with no error. `Start-Transcript` writes UTF-8 with a BOM instead, and the
`U+FEFF` in front of line one defeats a preamble match anchored at the start of the line, so the
`INFO - ` of line one returns as part of a metric name. Then someone ran the job by hand at
10:42 to check a fix, and its three records joined a series of 400.

```
$ python logsift.py --self-test
logsift self-test: no network, no database, no arcpy
--------------------------------------------------------------------
PASS  a thousands separator is read as one number
...
PASS  '2026-03-04 03:14:07 - INFO -' mines exactly one pair, seven seconds  <-- pinned defect
PASS  '2026-03-04 03:14:07 - INFO -' never mines an info_ key  <-- pinned defect
PASS  '2026-03-04 03:14:07 | INFO |' mines exactly one pair, seven seconds  <-- pinned defect
...
PASS  '[2026-03-04 03:14:07]' mines exactly one pair, seven seconds  <-- pinned defect
...
PASS  an ISO stamp with Z is preamble, not part of the metric key  <-- pinned defect
...
PASS  a logger's module field does not become part of the key
PASS  stripping the preamble does not swallow the payload  <-- pinned defect
PASS  a start time really does mine the year as a number  <-- pinned defect
PASS  and start_time is refused as a metric  <-- pinned defect
...
PASS  a second duration on one line cannot overwrite the first with a zero  <-- pinned defect
PASS  and the raw pass really does mine that zero, so the guard is load bearing  <-- pinned defect
PASS  because elapsed_time and duration are one series  <-- pinned defect
...
PASS  the clock matcher reserves its own label, so 'duration' is not also mined as zero  <-- pinned defect
...
PASS  a hand formatted clock with a one digit seconds field is still a clock, not the zero of its hours  <-- pinned defect
...
PASS  a real zero is mined: only a zero that is a clock is refused
...
PASS  and no supplied rule can invent a metric from it  <-- pinned defect
PASS  a metric really named error_count is not cut down to count  <-- pinned defect
...
PASS  a supplied rule beats a built-in one  <-- pinned defect
...
PASS  a rule written against the canonical key reaches the label too  <-- pinned defect
...
PASS  the csv is LF only, so Windows and Linux write the same bytes  <-- pinned defect
...
PASS  the log_file cell starting with a carriage return gets a leading apostrophe  <-- pinned defect
PASS  the sample_line cell starting with = gets a leading apostrophe  <-- pinned defect
...
PASS  a negative metric_value stays a number, with no apostrophe  <-- pinned defect
PASS  a text cell with a carriage return inside it stays one cell of one row, on every Python  <-- pinned defect
...
PASS  a utf-8 bom is found and skipped  <-- pinned defect
PASS  a utf-16le bom, as powershell 5.1 > and >> write, is found  <-- pinned defect
...
PASS  a utf-32le bom is not mistaken for the utf-16le bom it starts with  <-- pinned defect
...
PASS  Logs, logs, Log and log resolve to one candidate  <-- pinned defect
...
PASS  an ancestor working directory is refused  <-- pinned defect
PASS  so one shared log is not charted as every job's own  <-- pinned defect
PASS  an ancestor spelled with a .. is refused too  <-- pinned defect
PASS  an ancestor is refused even when the script's folder is not local  <-- pinned defect
PASS  Logs and LOGS are ONE directory where the filesystem folds case  <-- pinned defect
...
PASS  one unlistable folder among good ones is exit 3, and the good rows still print  <-- pinned defect
PASS  and an unlistable folder alone is exit 3, not 'nothing to chart'  <-- pinned defect
...
PASS  the real duration survives the whole pipeline  <-- pinned defect
...
PASS  and the start time never enters the file
...
PASS  ps51.log (utf-16-le) mines the same two metrics from two lines  <-- pinned defect
...
PASS  a utf-16 tail read in 61 byte blocks stays on character boundaries  <-- pinned defect
...
PASS  a utf-16 tail stops after a few blocks, not at the start of the file  <-- pinned defect
PASS  and every block starts a whole code unit after the bom, even when the block size is odd  <-- pinned defect
...
PASS  with --sections each section is its own series  <-- pinned defect
...
PASS  a run at 10:42 for a job scheduled at 03:14 is left out of the series  <-- pinned defect
...
PASS  a manual run whose first stamp is US dated is left out, not kept  <-- pinned defect
...
PASS  a long run read with --tail is judged by its first line, not its last  <-- pinned defect
...
PASS  and it is not counted as a log file that was read  <-- pinned defect
PASS  --apply is OFF by default  <-- pinned defect
...
PASS  a unique prefix of --apply is refused, not read as --apply  <-- pinned defect
...
PASS  a prefix of --scheduled-at is a usage error
PASS  a prefix of --tail is a usage error
...
PASS  one bad path among good ones fails the whole run  <-- pinned defect
...
PASS  stdout carries ONLY the data, so a redirect gives a clean csv  <-- pinned defect
...
PASS  --scheduled-at through main leaves manual runs out, counts them, and is exit 3 for a log it could not classify  <-- pinned defect
...
PASS  --out without --apply writes NOTHING  <-- pinned defect
...
PASS  and writes it INSTEAD of stdout, not as well as  <-- pinned defect
PASS  and writes exactly what the dry run printed  <-- pinned defect
...
PASS  and leaves the existing file untouched  <-- pinned defect
PASS  a write that fails is exit 2, not a false success
PASS  a log file named @job.log and lines that start with - or @ reach the csv guarded  <-- pinned defect
...
PASS  and the file --apply writes carries the same guard  <-- pinned defect
...
PASS  'Run time: 0:00:07' is seven seconds, never the zero of its hours  <-- pinned defect
...
PASS  'Updated: 2026-03-04 03:14:07' mines nothing  <-- pinned defect
...
PASS  milliseconds are converted to seconds  <-- pinned defect
...
PASS  'Total time: 1h 2m 3s' is 3723.0 seconds, the sum of its units  <-- pinned defect
...
PASS  ERROR COUNT keeps its first word  <-- pinned defect
...
PASS  'DB_PASSWORD' is blanked  <-- pinned defect
...
PASS  the value of the flag in '--password hunter2' is blanked  <-- pinned defect
...
PASS  the tail of 'config pwd=Hun;ter2XYZ R' is blanked too  <-- pinned defect
...
PASS  the tail of 'SDE_PWD=Hunter3Real foun' is blanked too  <-- pinned defect
...
PASS  a flag value that starts with a dash is blanked in 'run.exe --password -Ab'  <-- pinned defect
PASS  the credential in "headers={'Authorization': 'B" is blanked  <-- pinned defect
...
PASS  a secret with a semicolon in it never reaches metric_key, display_name or sample_line  <-- pinned defect
PASS  TOKX12 from csv never reaches metric_key or display_name  <-- pinned defect
...
PASS  no mined row carries the token or the password  <-- pinned defect
...
PASS  one unreadable log among good ones is exit 3, and the good rows still print  <-- pinned defect
...
PASS  --max-files keeps the newest only, and exits 3 because an older run was cut  <-- pinned defect
PASS  a log deleted between the listing and the read exits 3, not 0  <-- pinned defect
...
PASS  a folder the account cannot traverse is exit 3, not a PermissionError traceback  <-- pinned defect
PASS  a secret in a banner never reaches a key under --sections, in csv  <-- pinned defect
...
PASS  a csv with a check mark and an arrow reaches a cp1252 stdout as UTF-8, rc 0  <-- pinned defect
...
PASS  main warns when the line limit cut a log short, and exits 3, not 0  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  the self-test leaves no temporary directory behind
PASS  a failed run's footer names the count and each failure
PASS  a harness that missed a deliberate failure is named FATAL, not passed over  <-- pinned defect
PASS  importing the file runs nothing: main() is defined, not called
--------------------------------------------------------------------
479 assertions, 0 failed
```

The block above is an excerpt of a real run; the `...` lines stand for omitted lines, and the
last line is the real footer.

## Requirements

Python 3.9 or newer. Nothing to install, no `arcpy`, no third-party package, no network and no
database. It runs on ArcGIS Pro's Python and on a plain `python3` equally. The self-test above
runs 479 assertions on Windows with Python 3.13, 3.12, 3.10 and 3.9, and 479 on Linux with
Python 3.12. The counts are equal on all five. One label differs by platform:
`Logs and LOGS are ONE directory where the filesystem folds case` on Windows reads `... are TWO
directories where it does not` on Linux, because the test asserts what each filesystem does.
Branch coverage of `logsift.py` is 100 percent (1261 statements, 296 branches), measured with
`python -m coverage run --branch logsift.py --self-test`. `pyflakes` is clean, and the file
compiles with `python -W error`.

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

The tables below come from three synthetic nightly logs. This script writes them and sets each
file's modification time to 07:15 UTC on its day, which is the time the table shows. The files
are old by now, so these examples add `--days 36500`; leave that off for logs you are still
writing.

```python
import os, calendar
os.makedirs("parcels/Logs", exist_ok=True)
for i, day in enumerate((15, 16, 17)):
    ts = "2026-09-%02d 03:14:" % day
    lines = [ts + "00 - INFO - Start time: 2026-09-%02d 03:14:00" % day,
             ts + "01 - INFO - Batch size: 500",
             ts + "02 - INFO - Connected to the service",
             ts + "03 - INFO - Records checked: 12,000",
             ts + "04 - INFO - Loaded 12000 source records",
             ts + "05 - INFO - Records updated: %d" % (400 + i),
             ts + "06 - INFO - Verification: 3,000 features",
             ts + "07 - INFO - Duration: 0:00:07.13376%d" % (i + 1),
             ts + "08 - INFO - Done"]
    path = "parcels/Logs/nightly_202609%02d.log" % day
    open(path, "w", newline="\n").write("\n".join(lines) + "\n")
    t = calendar.timegm((2026, 9, day, 7, 15, 0))
    os.utime(path, (t, t))
```

```
$ python logsift.py parcels/Logs --days 36500
logsift: 3 log file(s), 27 line(s), 12 metric row(s), 0 error line(s)
metric_key            metric_value  observed_at_utc            log_file
--------------------  ------------  -------------------------  --------------------
checked_record_count  12000         2026-09-15T07:15:00+00:00  nightly_20260915.log
checked_record_count  12000         2026-09-16T07:15:00+00:00  nightly_20260916.log
checked_record_count  12000         2026-09-17T07:15:00+00:00  nightly_20260917.log
duration_seconds      7.133761      2026-09-15T07:15:00+00:00  nightly_20260915.log
duration_seconds      7.133762      2026-09-16T07:15:00+00:00  nightly_20260916.log
duration_seconds      7.133763      2026-09-17T07:15:00+00:00  nightly_20260917.log
source_record_count   12000         2026-09-15T07:15:00+00:00  nightly_20260915.log
source_record_count   12000         2026-09-16T07:15:00+00:00  nightly_20260916.log
source_record_count   12000         2026-09-17T07:15:00+00:00  nightly_20260917.log
updated_record_count  400           2026-09-15T07:15:00+00:00  nightly_20260915.log
updated_record_count  401           2026-09-16T07:15:00+00:00  nightly_20260916.log
updated_record_count  402           2026-09-17T07:15:00+00:00  nightly_20260917.log
```

Those logs also carry a start time, a batch size and a line reading
`Verification: 3,000 features`. None of the three is in the table. The first two are refused,
and the third needs a rule, because `verification` is not a word this tool charts on its own.

The data goes to stdout as UTF-8 bytes with LF line ends, and every message goes to stderr, so a
redirect gives a clean file. The `sample_line` column holds the log line as the log wrote it, with
any secret that has a name beside it blanked (see below):

```
$ python logsift.py parcels/Logs --days 36500 --format csv > metrics.csv
$ head -2 metrics.csv
metric_key,display_name,metric_value,observed_at_utc,log_file,sample_line
checked_record_count,Checked Record Count,12000,2026-09-15T07:15:00+00:00,nightly_20260915.log,"2026-09-15 03:14:03 - INFO - Records checked: 12,000"
```

Writing the file yourself needs `--apply`. Without it nothing is written:

```
$ python logsift.py parcels/Logs --days 36500 --format csv --out metrics.csv
would write 1835 byte(s) to metrics.csv (overwriting it). Nothing written: add --apply.
$ python logsift.py parcels/Logs --days 36500 --format csv --out metrics.csv --apply
wrote 1835 byte(s) to metrics.csv
```

| Flag | Default | What it does |
|---|---|---|
| `PATH` | none | Log files, or directories of them. A path that does not exist is a usage error. |
| `--script` | none | A job's script. Its own folder, and any `Logs` folder beside it, are searched. |
| `--working-directory` | none | The scheduled action's working directory, when it differs from the script's folder. |
| `--days` | `30` | Only log files modified in the last N days. |
| `--max-files` | `40` | At most N log files, newest first. A log in the day window that the limit cuts off is counted on stderr and the run exits 3. A value below 1 is a usage error. |
| `--max-lines` | `5000` | At most N lines per file. `0` reads the whole file. A file cut short by it is named on stderr. |
| `--tail` | off | Take `--max-lines` from the END of each file. Cannot be combined with `--sections`. |
| `--sections` | off | Prefix each key with the banner section it sits under, such as `=== Roads ===`. |
| `--scheduled-at` | none | `HH:MM` a job is scheduled to start. Repeat it for several. A run that started far from all of them is left out. |
| `--slack` | `30` | Minutes a scheduled run may start early or late. |
| `--rules` | none | A JSON file of your own label rules. Checked before the built-in ones. |
| `--format` | `table` | `table`, `csv` or `json`. |
| `--out` | none | Write the rendering to this file. With `--apply` it replaces stdout. Without `--apply` the dry run prints it to stdout as well. |
| `--apply` | off | Write the `--out` file. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

A file you name on the command line is read whatever its age. The day window applies to what
`--script` and a directory find for you.

## What it mines

Three shapes of line, in this order, and at most one value per canonical key per line:

- **A duration written as a clock.** `Duration: 0:00:07`, `Total time: 1:02:30`, `Run time:
  0:00:07`. Converted to seconds. Read first, so the clock is never read as the number zero.
- **A label and a number.** `Records checked: 12,000`, `Total execution time: 970.13 seconds`.
- **A verb and a number.** `Updated 1432 records`, `Deleted 0 features`.

A duration with a unit is converted to seconds too: `Processing time: 1432 ms` is 1.432,
`Duration: 12 minutes` is 720 and `Elapsed time: 2 hours` is 7200. A duration written in several
units is the sum of them: `Total time: 1h 2m 3s` is 3723, `Duration: 2h 30m` is 9000 and
`Duration: 1 hour, 5 minutes and 2 seconds` is 3902. The units are `ms`, `s`, `m`, `h` and their
long spellings. A unit beside any other label is left alone.

The label is then normalised to a key and mapped onto a canonical name, so that `Records
updated`, `Updated 1432 rows` and `updated_count` are one series rather than three. Eight
built-in rules cover the generic synonyms: duration, updated, deleted, inserted, checked,
processed, exported and source. Anything specific to your own jobs goes in a `--rules` file.

Each log file is one run, and the file's modification time is the observation time. One row per
file per metric, with the line the number came from, so a wrong number stays traceable.

That line is copied into the `sample_line` column of the csv and json. A line that holds a count
can also hold a credential, such as `GET .../query?token=...&f=json found 500 features`, so a
value after `token`, `password`, `passwd`, `pwd`, `secret`, `api_key`, `key`, `auth` or `sig`
(with `=` or `:`), after `Authorization:` or `Bearer`, and the password in `scheme://user:pass@`
is replaced with `REDACTED`. The long names, including `pwd`, match inside a longer word, so
`DB_PASSWORD=`, `SDE_PWD=`, `PGPASSWORD=`, `authToken=` and `x_api_key=` are covered, and a quoted
name such as `"token": "..."` in json or a Python dict is covered. `key`, `sig` and `auth` must
start a word, so `primary_key=5` is left alone. A quoted value may hold an escaped quote. An
unquoted value runs to the next space, and a comma, semicolon, bracket or ampersand ends it only
when a `name=` follows, so `token=abc&f=json` keeps its `f=json` and `pwd=Hun;ter2` leaves
nothing of `ter2` behind. The line is blanked before anything is mined from it, so a secret
cannot reach `metric_key` or `display_name` in those forms either. This is best effort. A secret
with no name beside it cannot be found, and neither can one with a space and no `=` or `:` after
its name (`password hunter2`). A value with a space in it that is not quoted ends at the space,
and the rest of it stays visible. A command line flag with a space before its value, such as
`--password hunter2`, `--client-secret hunter2` or the PowerShell form `-Password hunter2`, is
covered, and so is the same flag with a quoted value. A value that starts with a single dash,
such as `--password -Abc123`, is blanked as a value; only a word that starts with two dashes and a
letter is read as the next flag and kept. The credential after `Authorization` is blanked in a
quoted dict or JSON key, in a quoted value, and after `Bearer`, `Basic`, `Negotiate`, `Token`,
`NTLM`, `HOBA`, `Mutual`, `ApiKey` or `AWS4-HMAC-SHA256`; after `Digest` the rest of the line is
blanked, because its fields are comma separated. A JSON string held inside a JSON string, which
writes `\"password\": \"...\"`, is covered. An ODBC value in braces, `PWD={my pass;word}`, is
blanked whole. An AWS access key id (`AKIA` and 16 capitals or digits) is blanked by its shape
with no name beside it. The name list is fixed. A `Cookie:` header
(`sid=...`), `pass=`, `pat=`, `AccountKey=`, a URL-encoded name such as `token%3Dabc`, a short
flag such as `-p VALUE`, a bare `code=` OAuth value, `bearer=VALUE`, a `Set-Cookie:
sessionid=...` value and a token with no name beside it are not recognised. A banner under
`--sections` is blanked the same way, before it names a section. The text of a key built from
words that sit before a number is the log's own text, so a secret written as plain words there is
not covered.

```
$ python logsift.py tok.log --format csv
found_features,Found Features,500,...,tok.log,2026-09-17 03:14:03 - INFO - GET https://host/q?token=REDACTED&f=json found 500 features
```

## CSV and spreadsheets

A log line and a log file name are text that somebody else wrote, and the csv is meant to be
opened in a spreadsheet. A spreadsheet reads a cell that starts with `=`, `+`, `-`, `@`, a tab or
a carriage return as a formula
([OWASP, CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)). So in the csv,
every text cell that starts with one of those six characters gets one leading apostrophe. The
text columns are `metric_key`, `display_name`, `observed_at_utc`, `log_file` and `sample_line`.
`metric_value` is a number and is never changed, so a negative count stays `-7`.

A file named `@job.log` that holds the lines `-- Records updated: 5` and `+ Records checked: -7`
gives this csv. The real command and its real output, with the time column shortened:

```
$ python logsift.py @job.log --format csv
metric_key,display_name,metric_value,observed_at_utc,log_file,sample_line
checked_record_count,Checked Record Count,-7,<time>,'@job.log,'+ Records checked: -7
updated_record_count,Updated Record Count,5,<time>,'@job.log,'-- Records updated: 5
```

The guard covers the file that `--out FILE --apply` writes, because both come from the same
function. `--format json` and the table are not changed: neither one is opened in a spreadsheet.
This tool never reads its own csv back. A program that reads it removes exactly one leading
apostrophe from a text cell that starts with one.

A cell that holds a line break is a second risk. Python 3.9 and 3.10 do not quote a lone carriage
return when the line terminator is LF, so a reader ends the row there and the rest of the cell
becomes a new row that starts with whatever followed the break, past the guard. A row with a CR
or an LF in any cell is therefore written with every field quoted. Python 3.12 and 3.13 quote
such a field by themselves. The self-test pins the behaviour on every Python it runs under
(Requirements lists where it was run).

## Log encodings

A log is never assumed to be UTF-8. The first 8 KiB decide the encoding. You never choose it.

| First bytes | Read as | Written by |
|---|---|---|
| `EF BB BF` | UTF-8, BOM skipped | `Start-Transcript` in Windows PowerShell 5.1 |
| `FF FE` | UTF-16LE, BOM skipped | `>`, `>>` and `Out-File` in Windows PowerShell 5.1 |
| `FE FF` | UTF-16BE, BOM skipped | a big-endian writer |
| `FF FE 00 00` | UTF-32LE, BOM skipped | rare. Tested before UTF-16LE, whose BOM it starts with. |
| `00 00 FE FF` | UTF-32BE, BOM skipped | rare |
| no BOM, a zero in about every other byte | UTF-16LE or UTF-16BE, by which half holds the zeros | a tool that wrote UTF-16 and left out the BOM |
| anything else | UTF-8, undecodable bytes replaced | PowerShell 7, which writes UTF-8 without a BOM, and plain ASCII |

Zeros in both halves of the sample are a file padded with NUL bytes, and are not read as UTF-16.

Exercised for real on Windows PowerShell 5.1.26100 (`$PSVersionTable.PSVersion`). This script
wrote one log with `Start-Transcript` and two with `>`:

```powershell
function Lines($day, $start, $recs, $dur) {
  "$day $start - INFO - Start time: $day $start"
  "$day $start - INFO - Batch size: 500"
  "$day $start - INFO - Connected to the service"
  "$day $start - INFO - Records checked: 12,000"
  "$day $start - INFO - Loaded 12000 source records"
  "$day $start - INFO - Records updated: $recs"
  "$day $start - INFO - Verification: 3,000 features"
  "$day $start - INFO - Duration: $dur"
}
Start-Transcript -Path "$PWD\nightly_0304.log" | Out-Null
Lines '2026-03-04' '03:14:00' '1,432' '0:00:07.133764'
Stop-Transcript | Out-Null
Lines '2026-03-05' '03:14:00' '1,433' '0:00:08.100000' > nightly_0305.log
Lines '2026-03-05' '10:42:00' '3' '0:00:02.000000' > manual_1042.log
```

The first four bytes of each file were then read back:

```
$ for f in *.log; do echo $f $(head -c 4 $f | xxd -p); done
manual_1042.log fffe3200
nightly_0304.log efbbbf2a
nightly_0305.log fffe3200
```

`nightly_0304.log` also carries the transcript header, which is why it has more lines.
`manual_1042.log` starts at 10:42 and records three updates.

`nightly_0304.log` is the UTF-8 BOM transcript. The other two are UTF-16LE. All three mine:

```
$ python logsift.py . --scheduled-at 03:14
left out manual_1042.log as a manual run: it started at 10:42, 448 minute(s) from the nearest scheduled time
metric_key            metric_value  observed_at_utc                   log_file
--------------------  ------------  --------------------------------  ----------------
checked_record_count  12000         2026-10-13T21:21:43.922001+00:00  nightly_0304.log
checked_record_count  12000         2026-10-13T21:21:43.936085+00:00  nightly_0305.log
duration_seconds      7.133764      2026-10-13T21:21:43.922001+00:00  nightly_0304.log
duration_seconds      8.1           2026-10-13T21:21:43.936085+00:00  nightly_0305.log
source_record_count   12000         2026-10-13T21:21:43.922001+00:00  nightly_0304.log
source_record_count   12000         2026-10-13T21:21:43.936085+00:00  nightly_0305.log
updated_record_count  1432          2026-10-13T21:21:43.922001+00:00  nightly_0304.log
updated_record_count  1433          2026-10-13T21:21:43.936085+00:00  nightly_0305.log
logsift: 3 log file(s), 46 line(s), 8 metric row(s), 0 error line(s)
logsift: 1 manual run(s) left out of the series
```

The `>>` operator does not match the encoding of the file it appends to
([about_Character_Encoding](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-5.1)).
A UTF-8 log that a later `>>` extended in UTF-16LE is decoded by its start, so the appended
half is not mined. That is a named limit below.

## Tail read

`--tail` takes `--max-lines` from the end of each file, where a job writes its totals, and costs a
few blocks of 64 KiB rather than the whole file. The window is read backwards in whole code
units, so a UTF-16 or UTF-32 file is never decoded from the middle of a character. The first line
of the window is dropped when the window does not begin at the start of the file, because half a
line can mine half a number. The start time used by `--scheduled-at` is always read from the head
of the file. Lines above the window are never read and are not reported, so a metric written only
near the top of a long log is absent and the run still exits 0. That is the choice `--tail` makes.

Measured on a 163 MB UTF-16LE log of 1,500,000 lines, on the same Windows machine:

```
$ time python logsift.py big16.log --tail --max-lines 5
real    0m0.134s
$ time python logsift.py big16.log --max-lines 0
real    0m29.188s
```

Both mined `updated_record_count` 1500000. Error and warning line counts cover only the lines
that were read.

## Sections

A job that processes several layers often writes one banner per layer. Without `--sections` the
second layer's `Records updated` overwrites the first, because the last value in a file wins.
With it, a key under a banner is prefixed with the banner's name:

`sect.log` is five lines: `Records checked: 70`, then `=== Parcels ===`, `Records updated: 10`,
`=== Road Centerlines ===` and `Records updated: 5`.

```
$ python logsift.py sect.log --sections
metric_key                             metric_value  observed_at_utc                   log_file
-------------------------------------  ------------  --------------------------------  --------
checked_record_count                   70            2026-10-13T21:21:50.200651+00:00  sect.log
parcels.updated_record_count           10            2026-10-13T21:21:50.200651+00:00  sect.log
road_centerlines.updated_record_count  5             2026-10-13T21:21:50.200651+00:00  sect.log
```

A banner is a run of three or more of one fence character (`=`, `#`, `*`, `~` or `-`) on BOTH
sides of a title with a letter in it, with or without a log preamble. A one-sided `--- retrying`
is a message. A banner is counted as a line and is never mined. The title is cut at 40 characters.
A line above the first banner keeps its plain key. `--tail` and `--sections` together are a usage
error, because a tail window starts part way through a section.

## Manual runs

A series is for scheduled runs. `--scheduled-at 03:14` (repeat the flag for several times) reads
the first timestamp of each log, which is when the run started, and leaves out the rows of any run
that started more than `--slack` minutes (default 30) from every scheduled time. The distance is
measured round the clock, so a task scheduled at 23:50 that starts at 00:05 is 15 minutes late.
The run is still read and counted, stderr names it, and a run left out is never silent.
A log with no timestamp at its start cannot be told either way. It is kept, stderr names it,
and the run exits 3.
If every run was manual, the exit code is 1, not an empty success.

Python `logging` writes `asctime` in local time by default
([logging, LogRecord attributes](https://docs.python.org/3.12/library/logging.html)), so give
`--scheduled-at` the clock the log is written in, not UTC.

## What it refuses

- **The log level.** The preamble is stripped once, before anything is matched. `INFO`,
  `WARNING` and a timestamp never become part of a metric name. There is no later rule that
  strips a level word off a key, because that rule turns a real `error_count` metric into a
  series called `count`.
- **A number that is a clock.** `start_time`, `time` and `finished` are dropped by name. A
  duration written as a clock is converted rather than read digit by digit.
- **A number that is the front of a date or a clock.** After a label or a verb, `2026` of
  `Updated: 2026-03-04 03:14:07`, the `12` of `Last updated: 12/31/2025` and the `03` of
  `as of 2026-03-04 03:14:07` are not values. Without this a date line below the real count
  overwrites it with the year, because the last value in a file wins.
- **A level word that is part of a label.** `ERROR COUNT: 3` is the key `error_count`, and
  `WARNING count: 2` is `warning_count`. A level is stripped only inside brackets, before a
  separator (`ERROR: ...`, `INFO - ...`) or after a timestamp. `ERRORS: 4` is not the level.
- **A second value for a key already mined from that line.** The first value wins. This is the
  guard the flat-line defect needed, and deduplicating on the raw label is not enough for it.
- **A setting the job echoed back.** Any key ending in `batch_size` is configuration, not a
  measurement.
- **A hex build or commit id**, and `cpp` and `s_features`, which fall out of `arcpy` messages
  and traceback text.
- **A label with no measuring word in it.** `some_random_label: 5` and `Widgets: 12` are
  dropped. Give them a rule if you want them.
- **A shared working directory.** A scheduled action may declare a working directory that is an
  ancestor of the script's own folder. On one host, several tasks declared the shared
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
source_record_count    12000         2026-09-17T07:15:00+00:00  nightly_20260917.log
updated_record_count   402           2026-09-17T07:15:00+00:00  nightly_20260917.log
verified_record_count  3000          2026-09-17T07:15:00+00:00  nightly_20260917.log
```

A rule is also how a task name before a label joins the shared series. `Parcels - Records
updated: 500` is its own series, `parcels_records_updated`, because the text before the label is
part of it. `{"^parcels_records_updated$": "updated_record_count"}` maps it onto the shared one.

Your rule is tried against the label as the log spells it, and again against the canonical key a
built-in rule produced. Both `^records_checked$` and `^checked_record_count$` reach that series,
so a rule written against the name you can see in the output does what it looks like it does.

A rule you supply is taken at its word. It skips the noise list and the vocabulary filter,
because you named the key yourself. That is how `verification` above gets mined at all.

## Exit codes

0 metrics mined, 1 nothing to chart, 2 the write step failed, 3 the series is known to be
incomplete or unclassified (a log file could not be read, a folder could not be listed,
`--max-lines` cut a log short, or `--scheduled-at` could not classify a log), 64 usage error. Every usage error exits 64, including one that
the argument parser raises, such as an unknown flag or `--days x`.

Exit 1 is the interesting one. It means the tool read log files and no number in them survived
the rules, or found no log file inside the day window. Both are the failure this tool exists to
make visible, so neither is reported as success.

Exit 3 means at least one log file could not be opened, for example because the running job holds
it locked. The readable files are still mined and printed, stderr names the file, and the run
exits 3 so a scheduled wrapper does not take a missing run for a complete series. Exit 3 is
checked before exit 1, so an unreadable file is never blamed on the rules. A folder that cannot
be listed is the same failure and the same exit: every run in it is missing, so the run exits 3
even when other folders gave rows, and also when no other folder did.

Exit 3 is also the answer when the tool knows a result is incomplete and would otherwise print it
as clean. With the default `--max-lines 5000` and no `--tail`, a log longer than the limit was read
only to the limit, a total written last was never read, and the run says so and exits 3. Use
`--tail` or `--max-lines 0` and the same log exits 0. Under `--scheduled-at`, a log whose first
stamp the tool cannot read (no stamp, or one that is not ISO or `MM/DD/YYYY`, such as a time
only `10:42:07 INFO ...`) cannot be told from a scheduled run, so it is kept, named on stderr, and
the run exits 3. A US dated stamp (`08/11/2026 10:42:07`, with or without `AM` or `PM`) is read.

A log file that is deleted between the listing and the read, or during the listing, is named on
stderr and the run exits 3, because its run is missing from the series. The same exit follows when
`--max-files` cuts off older logs inside the day window: stderr counts them, and `--max-files`
or `--days` is the fix. A file that opens and then fails during the read, such as a share that drops, ends the
run with an `OSError` traceback and exit 1, the same code as nothing to chart.

## Why not Promtail, mtail, or grep

Promtail's metrics stage, Datadog's log-based metrics and Splunk all do this properly, with a
query language, storage, alerting and a history behind them. If you already run one, use it.
[mtail](https://github.com/google/mtail) (Apache 2.0) and
[grok_exporter](https://github.com/fstab/grok_exporter) (Apache 2.0) also turn unstructured log
lines into Prometheus metrics, with far more pattern power than this tool has. They are the
better choice when a Prometheus server already exists.

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
- A metric's value is the last one in the file that was read. Without `--tail` that is the last
  one in the first `--max-lines` lines (5000 by default), and stderr says so and the run exits 3
  when the cap cut the file short. A job that writes its totals at the end of a long log needs `--tail` or
  `--max-lines 0`. A job that reports the same key for two different things gets one row and the
  second value.
- A sentence count takes the words after the number as its key, so `Updated 1432 records in the
  parcels layer` charts as `updated_records_in_the_parcels_layer`. Write the label form, or give
  that phrasing a rule.
- Only `.log` and `.txt` are read from a directory. A `.csv` or `.json` beside the logs is
  somebody's data, and mining it would invent metrics out of column values.
- The observation time is the file's modification time, not a timestamp parsed out of the lines.
  A file appended to all day carries the time of its last write. One file per run is the shape
  this fits.
- No alerting and no thresholds. It does not know that 402 is a reasonable number or that zero
  is not. It only refuses to mine a number that was never a measurement.
- `--out` with `--apply` overwrites any file you name, including a log you are mining, so do not
  point it at your logs. Without `--apply` nothing is written, and the dry run says whether the
  file is already there. An empty `--out ""` counts as no `--out`: the data goes to stdout and
  nothing is written. `--apply` with no `--out` does nothing and says nothing.
- A log with no BOM is sniffed from its zero bytes, which works for UTF-16 text that is mostly
  ASCII. UTF-16 text that is mostly non-Latin has few zero bytes, is read as UTF-8 and mines
  nothing. UTF-32 with no BOM is not detected and mines nothing. A real PowerShell 5.1 log always
  has its BOM, so this has not met a real file.
- An encoding is chosen once, from the start of the file. A UTF-8 log that a later `>>` extended
  in UTF-16LE mines only its UTF-8 part. Microsoft documents that `>>` does not match the
  encoding of its target.
- `--tail` and `--sections` cannot be combined, and a job total written after the last banner is
  keyed to that last section.
- `--scheduled-at` uses the first timestamp in the log, in the log's own clock, and a run that
  crosses a daylight saving change is judged by the clock it wrote. A first stamp that is not a
  real time of day (hour 24 or more) is treated as no timestamp.
- A line that carries a second log line inside it is not split, and it is mined like any other.
  A `Start-Transcript` header line `Host Application: ... "Records updated: 1"` mines
  `updated_record_count = 1` from the command text, and a quoted `... - INFO - Duration: 0:00:07`
  mines `duration_seconds = 7`. A quoted line with a level in front of the label gives the key
  `info_records_updated`, because the level is inside the line, not at its start. A job that
  echoes its own command line into its log can therefore add or overwrite a value.
- The tail read decodes the whole window again after each block it adds. That is fine for the
  default 5000 lines, and slow for a `--max-lines` in the millions.
- `--script` reads every `.log` and `.txt` file in the script's own folder and its `Logs` folder.
  A shared scripts folder mixes other jobs' logs, and a `notes.txt`, into this job's series. A
  script path in the other platform's style (`C:\jobs\x.py` given on Linux) has the parent `.`,
  so the logs under the current directory are mined as that job's logs.
- A file named twice, or a file and its directory, is read twice and its rows are doubled.
  Directories are de-duplicated and named files are not.
- With `--sections`, a banner whose title holds a number, such as `=== Total records updated: 99
  ===` or `=== Layer 3 of 12: Roads ===`, is a section header and its value is never mined. Its
  digits also join every key below it (`total_records_updated_99.checked_record_count`), which
  can split one series into several. A metric line wrapped in a banner, such as `*** Records
  updated: 5 ***`, is eaten the same way. A banner whose first 40 characters hold no letter or digit
  has an empty section name, and its keys become `.updated_record_count`. A line over 1000
  characters is never a banner.
- With `--scheduled-at`, a log whose first stamp is neither `YYYY-MM-DD HH:MM:SS` nor
  `MM/DD/YYYY HH:MM:SS` (for example a time only `10:42:07 INFO ...`) cannot be classified. It is
  kept, stderr names it, and the run exits 3, so a manual run among them is not taken for a clean
  series.
- A time only preamble such as `10:42:07 INFO Records updated: 3` is not stripped, so the level
  word stays in the key (`info_records_updated`) and starts a separate series from the dated form
  of the same line.
- A label with an accented letter in a cp1252 log decodes that letter as U+FFFD, so the key is
  spelled differently from the same label in a UTF-8 log (`parcels_chou_s_count`).
- A decimal comma or a dotted thousands separator is read as an English number: `1.432` is 1.432
  and `1,5` is 15. Logs from a locale with those separators are misread.
- A duration of a day or more is misread. `Duration: 1 day, 2:03:04` (Python `timedelta`) is 1
  second, and `Duration: 1.02:03:04` (.NET `TimeSpan`) mines nothing.
- A number of about 400 digits becomes infinity and prints as `inf` in the table and `Infinity`
  in the json, which is not valid json.
- `--days` above about 739000 stops with an `OverflowError` traceback. A negative `--max-files`
  such as `-1` silently drops the oldest file. `--max-files 0` and a negative `--days` both end
  with `no log file in the last N day(s)` and exit 1, and the message does not name the flag.
  `--script` with a path that does not exist and no logs elsewhere says `pass a log file or a
  directory of them, or --script`, although `--script` was given.
- A line is read whole. One line of 30 MB with no newline took 78 seconds, because the line limit
  counts lines, not bytes.
- The data is written to stdout in one call, so a closed pipe such as `| head -1` ends a large
  run with a `BrokenPipeError` traceback and exit 1 after the metrics were mined.
- In Windows PowerShell 5.1, `>` re-encodes the redirected output as UTF-16LE with a BOM, after
  decoding logsift's UTF-8 bytes with the console code page, so a non-ASCII character can be
  garbled. It depends on the console output encoding: with the default code page on one test host
  a check mark and an arrow came through intact, and with the code page set to CP437 they did not.
  Use `--out FILE --apply` to get UTF-8.
- The csv guard is the six characters above and nothing more. OWASP also lists a line feed and the
  full-width forms of `=`, `+`, `-` and `@`. A cell cannot start with a line feed that came from a
  log line, because lines are split there. A log file name that starts with a line feed, or with a
  full-width sign, is not guarded. OWASP also warns that its prefix advice is "not reliable in
  Microsoft Excel after saving and re-opening the CSV file". Treat a csv built from logs you do
  not control as untrusted, and open it as text, not by double click.
- The apostrophe is visible. A cell whose real text already begins with `'` and then a formula
  character is not changed, and a consumer cannot tell it from a cell that was guarded. A text
  cell that starts with `=`, `+`, `-` or `@` and was meant to be literal, such as the
  `sample_line` `-- Records updated: 5`, shows a leading `'` in a program that does not strip it.
- `--rules` patterns are ordinary Python regular expressions from a file you supply. A pattern
  with catastrophic backtracking, such as `(a+)+$` against the key `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaab`,
  hangs the run. A rules file saved as UTF-8 with a BOM, or as UTF-16 (PowerShell 5.1 `>`), is
  refused with exit 64. The tool sniffs the encoding of logs but not of the rules file.
- The csv guard tests only the first character. Every log line is stripped before it becomes a
  `sample_line`, so that column never starts with a space and the guard sees its first sign. The
  `log_file` column is the one that can start with spaces, because a file name on Linux may. A name
  such as `  =1+1.log` is not guarded.
- A log file name is never redacted. It goes to stdout, to the csv and json, and to stderr as it
  is, so a secret placed in a file name is printed. A control character or an ANSI escape in a
  file name is printed raw in the table and on stderr, and only the csv guards the first character.
- `| head -1` ends the run with a `BrokenPipeError` traceback and exit 1, the same code as
  `nothing to chart`, so exit 1 is ambiguous when the output goes through a pipe that can close.
- A line whose only number is an HTTP status in an error line, such as `Error: 404 Not Found`, is
  charted as the metric `error = 404`. The tool refuses a number that was never a measurement
  only by label, not by meaning.
- `Elapsed: 00:01:07` and `Elapsed: 0:07:13` mine nothing, because `elapsed` alone is not in the
  list of duration labels. The labels read as durations are `duration`, `total duration`,
  `elapsed time`, `processing time`, `total execution time`, `total time` and `run time` (also
  `runtime`). This is a missed value, not a wrong one, and a `--rules` file adds a label.
- A sentence count keeps the words after the number in its key, and a total in those words changes
  the key: `Processed 500 of 10000 records` is `processed_of_10000_records`, a new series when the
  total changes.
- A log file that is deleted between the folder listing and the modification time check is dropped with no
  warning. Only a failure of the later read is warned about.
- When `--scheduled-at` removes every row as a manual run, the message `no number in those logs
  survived the rules` names the wrong cause. Exit 1 is still right. Read the line `manual run(s)
  left out of the series` above it.
- A path named beneath a folder the account may not enter is reported as `no such file or
  directory` with exit 64, not as exit 3, because the operating system answers the same way for
  both. A folder found through `--script` or `--working-directory`, or a named folder that is
  itself unreadable, exits 3.
- `--apply` with no `--out` writes no file and says nothing. The data goes to stdout and the run
  exits 0, so a scheduled task that forgets `--out` looks successful.
- The table and the json are not guarded, by design. Only the csv is meant to be opened in a
  spreadsheet. A table pasted into a spreadsheet gets no protection.
- With `--out FILE --apply`, an incomplete or empty result still overwrites the previous file. An
  unreadable log gives a csv that lacks that run, and a run whose rows are all filtered out
  writes a header-only csv and exits 1. Keep a copy of the last good csv.
- With `--out` and no `--apply`, the run exits 0 and writes no file, so a scheduled command that
  forgets `--apply` looks successful. stderr says `Nothing written`, and the data goes to stdout.
- The vocabulary filter is a word list. A job that counts "widgets" needs a rule, and there is no
  way for the tool to guess that one for you.
- Naming a csv file on the command line, for example `logsift metrics.csv`, mines it as ordinary
  log text and keeps no apostrophe strip. The tool does not parse its own csv back, but it will
  read one if you name it. The exclusion of `.csv` files applies only to a directory scan.
- The tail timing figures above (0.134 s for `--tail` and 29.188 s for a full read of a 163 MB
  UTF-16LE file) are machine dependent. A second measurement on the same machine gave 0.115 s and
  52.6 s, so the full read can take much longer than the figure quoted.
- The list of same-named repositories in the Name section depends on a GitHub search made on
  2026-11-11. A quick second search found the same kinds of projects, but the list will drift.
- Promtail, Datadog and Splunk are named in the comparison with no source link, so that
  comparison is the author's description and not a checked fact. mtail and grok_exporter are
  linked.
- Space grouped thousands are not read: `Records updated: 12 345` charts 12. Only comma grouping
  is handled.
- A duration written in several units with no clock is summed (`Duration: 7m 13s` is 433), but
  `Duration: 1h2m3s`, with no spaces between the pieces, writes no row and no warning.
- A file that begins with the bytes `FF FE` but is not UTF-16, for example a cp1252 log whose first
  line starts with those bytes, is read as UTF-16LE and mines nothing.
- A count of about 400 digits prints `inf` in the csv `metric_value` column too. A spreadsheet
  reads that as text.
- The self-test assertion about folded case expects a case sensitive filesystem on every platform
  except Windows, so it fails on a default case insensitive macOS volume. This was reasoned from
  the code, not run.
- Sources does not cite Windows case folding, NTFS 8.3 short names or Python csv quoting rules.
  The same-name repository claim was not re-checked against a live page. The `Start-Transcript
  -Append` claim was re-read on the PowerShell 5.1 page on 2026-11-11: with `-Append` and a target
  that has no byte order mark, it defaults to ASCII encoding.
- `Key: 5` redacts the count, because a bare `key` followed by a colon is treated as a secret.
  A flag that merely ends in a secret word, such as `--primary-key 5`, is redacted too. Both are
  false positives, not leaks.
- A secret whose value holds a space, such as `PGPASSWORD=Hunter 2XYZ`, leaves the part after the
  space visible, and the word after it can reach `metric_key`.
- A secret written as plain words with no `=` or `:` after its name, such as `Using token VALUE`,
  `Token acquired: VALUE`, `with password VALUE` or `Got token VALUE expires in 60 seconds`, stays
  visible in `sample_line`.
- A short or plural secret name is not on the fixed list: `pw=`, `cred=`, `tok=`,
  `subscription_key=`, `tokens:`, `Passwords:` and the SQL tool flag `-P VALUE` are not blanked.
- A password that holds an `@` in `scheme://user:pa@ss@host` leaves the part after the first `@`
  visible, and `--password --Abc` is read as a flag followed by a flag, so `--Abc` is kept.
- `AWS Access Key ID: AKIA...` is blanked by the shape of the id only. A secret key written beside
  it with no name is not found.
- The csv guard tests the first character against the six listed ones. A log file name or cell
  that starts with a line feed, spaces before an `=`, or a full-width sign is written unguarded.
- A sentence prefixed label splits a series: `Step 2 records updated: 5` is keyed
  `step_2_records_updated`, not `updated_record_count`. A progress line such as `Checked 5 of 10
  records` mines the stray key `checked_of_10_records`, a bare `Elapsed: 1.5 min` is converted and
  then dropped by the vocabulary filter (`Elapsed time: 1.5 min` works), and a count written after
  the verb (`250 features added`) is not mined.
- `--days` with a very large number (1000000 or more) ends in an `OverflowError` traceback and
  exit 1, the same code as nothing to chart.
- A folder that can be read but not searched (Linux mode 444) lists its files and then ends in a
  `PermissionError` traceback and exit 1, where mode 000 gives the exit 3 path.
- A single log line of 3 MB takes about 7 seconds to mine, because the patterns run over the whole
  line before it is cut to 240 characters.
- `--out FILE --apply` opens the file for writing directly, so a write that fails part way, such as
  a full disk, truncates the previous good csv. The write is not atomic.
- The claim that logsift runs on ArcGIS Pro's Python was exercised under the conda environment
  `asirpro` (Python 3.11.8), not under a Pro installation's own interpreter.

## Name

This list of same-named repositories was read from a GitHub search on 2026-11-11 and changes
over time. Other GitHub repositories are called `logsift`, among others: command line filters and
summarisers for access logs, severity levels and JSONL, an MCP log-noise tool, an LLM prompt
builder, a Java toolkit and a streaming anomaly detector. This one mines metrics from a scheduled
job's log files. It does not share code with any of them.

## Sources

Behaviour this tool relies on, and where it was checked:

- Windows PowerShell 5.1 `>`, `>>` and `Out-File` write UTF-16LE, `Start-Transcript` writes UTF-8
  with a BOM, any Unicode encoding except UTF-7 writes a BOM, and `>>` does not match the
  encoding of an existing file:
  [about_Character_Encoding](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_character_encoding?view=powershell-5.1).
  The first two were also read back from real files, shown above.
- `Start-Transcript -Append` matches an existing BOM and otherwise writes ASCII:
  [Start-Transcript](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.host/start-transcript?view=powershell-5.1).
- BOM byte sequences, including that the UTF-32LE BOM `FF FE 00 00` begins with the UTF-16LE BOM
  `FF FE`: [Unicode FAQ, UTF and BOM](https://www.unicode.org/faq/utf_bom.html).
- Python `logging` writes `asctime` as `2003-07-08 16:49:45,896` in local time by default:
  [logging](https://docs.python.org/3.12/library/logging.html).
- A spreadsheet runs a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return as a
  formula, and a leading single quote is one of the listed mitigations, with the Excel
  re-opening caveat quoted in Limits:
  [OWASP, CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection).
  Read on 2026-11-11. The page lists a line feed and full-width signs as well, which this tool
  does not guard.
- Python 3.9.25 and 3.10.20 leave a lone carriage return unquoted under an LF line terminator,
  and 3.12 and 3.13 quote it. This was measured, not read: with the whole-row quoting removed,
  the self-test fails on 3.9.25 and 3.10.20 and passes on 3.12 and 3.13.

No Esri, SQL Server or Census behaviour is used.

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
