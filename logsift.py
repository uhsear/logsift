#!/usr/bin/env python
"""Mine a scheduled job's own log files into a metric series, and refuse the number that is really a timestamp.

The charts were flat at zero for a month and nobody noticed, because they were
not empty. They were confidently wrong. The line was:

    2026-08-11 03:14:07 - INFO - Duration: 0:00:07

A label-and-number matcher reads the log level as part of the metric name and
mines info_duration = 0.0 from the leading zero of the clock. The real value,
7.13, is mined too. Both collapse onto the same canonical key, the zero arrives
second, and the series charts a job that appears to finish instantly for ever.

Promtail's metrics stage and Datadog's log-based metrics do this properly, with
a query language, storage and alerting behind them. They also want an agent on
the host, a server to ship to, and a pipeline you maintain. On a Windows box
where six scheduled scripts each write their own Logs folder, there is no agent
and nobody is going to install one. This tool is a 30 second answer for that
box: point it at the folder, get a CSV, chart it in a spreadsheet.

The part it does that grep and awk do not is refusal. A number in a log line is
often not a measurement: it is a clock, a log level, a batch size or a hex
build id. Every rule here exists because a specific wrong number reached a
chart.

    python logsift.py C:\\jobs\\parcels\\Logs
    python logsift.py --script C:\\jobs\\parcels\\nightly.py
    python logsift.py logs --format csv --out metrics.csv --apply
    python logsift.py --self-test

Data goes to stdout, every message goes to stderr, so a redirect gives a clean
file. Exit codes: 0 metrics mined, 1 nothing to chart (no recent log file, or
no number in them survived the rules), 2 the write step failed, 64 usage error.
"""

from __future__ import print_function

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Only these are read. A .csv or .json beside the logs is somebody's data, not
# a log, and mining it would invent metrics from column values.
LOG_SUFFIXES = (".log", ".txt")

# Subdirectory names checked beside a script. Windows folds case, so all four
# can be the same directory on one host and four different ones on another.
LOG_DIR_NAMES = ("Logs", "logs", "Log", "log")

DEFAULT_DAYS_BACK = 30
DEFAULT_MAX_FILES = 40
DEFAULT_MAX_LINES = 5000

# Strip the logging preamble before mining a line. Without this the label
# matcher sees the level word as part of the metric name: "... - INFO -
# Duration: 0:00:07" yields the real duration AND a junk info_duration = 0.0,
# the "0" of "0:00:07". Both canonicalize onto duration_seconds, so the zero
# overwrites the real value and the task charts a flat 0 for ever. The same
# cause turned "Start time: 2026-.." into a metric of 2026.
#
# Handles a timestamp with or without a level, and a bare level with no
# timestamp: "ts - LEVEL -", "ts | LEVEL |", "ts LEVEL -", "[ts]", "ts",
# "LEVEL -" and "LEVEL:". Every part is optional, so an ordinary line matches
# the empty string at position 0 and keeps all of its text.
LOG_PREAMBLE_RE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]?)?"
    r"\s*(?:[-|:]\s*)?"
    r"(?:\[?(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]?)?"
    r"\s*(?:[-|:]\s*)?"
)

# A duration written as a clock. Matched before the label matcher so that
# "Duration: 0:00:07" is read as seven seconds and never as zero.
HMS_DURATION_RE = re.compile(
    r"(?i)\b(?P<label>duration|elapsed time|processing time|total execution time|total time)\s*:\s*"
    r"(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2}(?:\.\d+)?)"
)
# The seconds field takes one digit or two, like the minutes field. A logger
# that formats its own clock rather than printing a timedelta writes
# "Duration: 0:00:7". Requiring two digits there makes this matcher miss the
# line, the label matcher reads it instead, and the metric is the "0" of the
# hours field: the flat zero this whole tool exists to refuse, arriving through
# the one shape of clock the clock matcher did not recognise.

# "Records updated: 1,432". The label may not contain a pipe, so a logger's
# module field in "ts | INFO | update_parcels | Records checked: 47285" is left
# out of the key.
LABEL_VALUE_RE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z0-9 /()_-]{2,80}?)\s*:\s*(?P<value>-?\d[\d,]*(?:\.\d+)?)\b"
)

# "Updated 1432 records". The same measurement written as a sentence.
VERB_VALUE_RE = re.compile(
    r"(?i)\b(?P<lemma>added|removed|updated|processed|exported|loaded|copied|retired|deleted|inserted|"
    r"appended|downloaded|checked|found|created|committed|reconciled)\b"
    r"(?:\s+(?P<value>\d[\d,]*(?:\.\d+)?))?"
    r"(?:\s+(?P<object>[A-Za-z][A-Za-z0-9 _/-]{0,60}))?"
)

# Keys that look like measurements and are not. Each of these reached a chart.
# start_time and time are clocks. finished, failed and runtimeerror are words
# that happened to sit beside a number. s_features and cpp come out of arcpy
# and traceback text. A batch size is a setting the script echoed back, not
# something it measured. The last pattern is a build or commit id.
NOISE_METRIC_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^start_time$", r"^time$", r"^finished$", r"^failed$", r"^runtimeerror$",
        r"^cpp$", r"^s_features$", r"^pass_\d+_complete$",
        r"^.*batch_size$",
        r"^[a-f0-9_]{16,}$",
    ]
]

# The nouns a job uses for the same thing. "500 records", "500 rows" and
# "500 features" are one series, and a phrasing change in the monitored script
# must not split it into two.
_NOUN = r"(?:records?|rows?|features?)"

# Collapse synonyms onto one canonical key so a metric charts as a single
# series. Eight rules, all generic. Anything specific to your own jobs belongs
# in a --rules file, which is checked first and wins.
CANONICAL_METRIC_RULES = [
    (re.compile(pattern), canonical) for pattern, canonical in [
        (r"^(?:duration|elapsed_time|processing_time|total_time|total_execution_time|run_time|runtime)$",
         "duration_seconds"),
        (r"^(?:updated|%s_updated|updated_%s|updated_total|updated_count|updated_in_\d+)$" % (_NOUN, _NOUN),
         "updated_record_count"),
        (r"^(?:deleted|removed|%s_(?:deleted|removed)|(?:deleted|removed)_%s|total_deleted|deleted_count|removed_count)$" % (_NOUN, _NOUN),
         "deleted_record_count"),
        (r"^(?:added|inserted|appended|%s_(?:added|inserted|appended)|(?:added|inserted|appended)_%s|added_count|inserted_count)$" % (_NOUN, _NOUN),
         "inserted_record_count"),
        (r"^(?:checked|%s_checked|checked_%s|checked_count)$" % (_NOUN, _NOUN),
         "checked_record_count"),
        (r"^(?:processed|%s_processed|processed_%s|processed_count)$" % (_NOUN, _NOUN),
         "processed_record_count"),
        (r"^(?:exported|%s_exported|exported_%s|total_%s_exported|exported_count)$" % (_NOUN, _NOUN, _NOUN),
         "exported_record_count"),
        (r"^(?:loaded_source_%s|source_%s|source_count|loaded_%s|%s_loaded|loaded_count)$" % (_NOUN, _NOUN, _NOUN, _NOUN),
         "source_record_count"),
    ]
]

# A key with none of these words in it is not a measurement anybody charts.
# This is the filter that drops "some_random_label: 5" and "widgets: 12".
# A rule you supply skips it, because you named the key yourself.
MEANINGFUL_METRIC_TOKENS = (
    "count", "record", "row", "parcel", "segment", "feature", "version",
    "duration", "second", "minute", "updated", "deleted", "removed", "added",
    "appended", "exported", "download", "processed", "checked", "created",
    "retired", "reconciled", "change", "null", "buffer", "error", "warning",
)

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

ROW_FIELDS = ("metric_key", "display_name", "metric_value", "observed_at_utc",
              "log_file", "sample_line")
FORMATS = ("table", "csv", "json")


# ----------------------------------------------------------------- pure core

def parse_number(text):
    """A log's number, thousands separators and all, or None."""
    clean = str(text).replace(",", "").strip()
    if not clean:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def normalize_metric_key(label):
    """A free text label as a key: lowercase, underscores, at most 80 characters."""
    clean = re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower()).strip("_")
    return clean[:80] if clean else ""


def canonicalize_metric_key(metric_key, rules=()):
    """Map a raw label to a canonical metric key, or None if it is not a metric.

    Rules you supplied are checked first and returned as given. They skip the
    noise list and the vocabulary filter below, because naming the key is the
    whole point of supplying one. The built-in rules do not skip either.

    A supplied rule is tried against the label as the log spells it and again
    against the canonical key a built-in rule produced, so "^records_checked$"
    and "^checked_record_count$" both reach the same series. Without the second
    try, a rule written against the name you can see in the output would look
    correct and quietly do nothing.
    """
    key = normalize_metric_key(metric_key)
    if not key:
        return None

    for pattern, canonical in rules:
        if pattern.match(key):
            return canonical

    for pattern, canonical in CANONICAL_METRIC_RULES:
        if pattern.match(key):
            key = canonical
            for supplied_pattern, supplied_canonical in rules:
                if supplied_pattern.match(key):
                    return supplied_canonical
            break

    for pattern in NOISE_METRIC_KEY_PATTERNS:
        if pattern.match(key):
            return None

    # No blanket "strip a leading level word" rule here. The level is removed
    # once, at the preamble, and never patched up afterwards: a rule that
    # stripped "error_" from a key would turn a real error_count metric into a
    # series called "count" and merge it with every other bare count.
    if not any(token in key for token in MEANINGFUL_METRIC_TOKENS):
        return None
    return key


def extract_metrics_from_line(line):
    """Raw (label, value) pairs for one line, before any canonical rule.

    The preamble is stripped first. Within one line a raw label is mined once.
    """
    line = LOG_PREAMBLE_RE.sub("", line, count=1)
    results = []
    seen = set()

    for match in HMS_DURATION_RE.finditer(line):
        total = (float(match.group("hours") or 0) * 3600.0
                 + float(match.group("minutes") or 0) * 60.0
                 + float(match.group("seconds") or 0))
        if "duration_seconds" not in seen:
            seen.add("duration_seconds")
            seen.add(normalize_metric_key(match.group("label")))
            results.append(("duration_seconds", total))

    for match in LABEL_VALUE_RE.finditer(line):
        label = normalize_metric_key(match.group("label"))
        value = parse_number(match.group("value"))
        if label and value is not None and label not in seen:
            seen.add(label)
            results.append((label, value))

    for match in VERB_VALUE_RE.finditer(line):
        value = parse_number(match.group("value") or "")
        if value is None:
            continue
        object_text = normalize_metric_key(match.group("object") or "")
        label = match.group("lemma").lower()
        if object_text:
            label = label + "_" + object_text
        label = normalize_metric_key(label)
        if label and label not in seen:
            seen.add(label)
            results.append((label, value))

    return results


def mine_line(line, rules=()):
    """Canonical (key, value) pairs for one line. At most one pair per key.

    Deduplicating on the RAW label is not enough, and that is the whole defect
    this tool exists for. "Duration: 0:00:07 Elapsed time: 0:01:00" mines
    duration_seconds = 7.0 from the clock matcher and elapsed_time = 0.0 from
    the label matcher, which reads the "0" of the second clock. The two raw
    labels differ, so a raw guard passes both, and elapsed_time canonicalizes
    onto duration_seconds. The zero arrives second and wins. The first value
    for a canonical key is the one kept.
    """
    results = []
    seen = set()
    for raw_key, value in extract_metrics_from_line(line):
        canonical = canonicalize_metric_key(raw_key, rules)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        results.append((canonical, value))
    return results


def title_case_metric(metric_key):
    return " ".join(part.capitalize() for part in str(metric_key).split("_") if part)


def load_rules(text):
    """Compile a JSON rules document into [(pattern, canonical_or_None)].

    The document is an object. Each key is a regular expression matched against
    a normalized label; each value is the canonical key to use, or null to drop
    that label as noise. Order is the order in the file.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ValueError('must be a JSON object of {"pattern": "canonical_key"}')

    rules = []
    for pattern, canonical in data.items():
        if canonical is not None:
            if not isinstance(canonical, str) or not normalize_metric_key(canonical):
                raise ValueError("rule %r: the canonical key must be a non-empty "
                                 "string, or null to drop the label" % pattern)
            canonical = normalize_metric_key(canonical)
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError("rule %r is not a valid regular expression: %s" % (pattern, exc))
        rules.append((compiled, canonical))
    return rules


def format_value(value):
    """1432.0 prints as 1432; 7.133764 keeps every digit it was given."""
    number = float(value)
    if number.is_integer() and abs(number) < 1e15:
        return "%d" % int(number)
    return "%s" % number


def render(rows, fmt="table"):
    """The metric series as text. Pure: no file, no clock, no environment."""
    if fmt == "json":
        return json.dumps(rows, indent=2, sort_keys=True)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(ROW_FIELDS)
        for row in rows:
            writer.writerow([format_value(row[field]) if field == "metric_value"
                             else row[field] for field in ROW_FIELDS])
        return buf.getvalue()

    columns = ("metric_key", "metric_value", "observed_at_utc", "log_file")
    cells = [[str(column) for column in columns]]
    for row in rows:
        cells.append([format_value(row[column]) if column == "metric_value"
                      else str(row[column]) for column in columns])
    widths = [max(len(line[i]) for line in cells) for i in range(len(columns))]
    out = []
    for index, line in enumerate(cells):
        out.append("  ".join(line[i].ljust(widths[i]) for i in range(len(columns))).rstrip())
        if index == 0:
            out.append("  ".join("-" * widths[i] for i in range(len(columns))))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------ io

def _emit(message):
    """Every message goes to stderr, so stdout carries only the data."""
    sys.stderr.write("%s\n" % message)


def _clean_text(value):
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _utc_now():
    return datetime.now(timezone.utc)


def _mtime(path):
    """Modification time, or None when the file is gone.

    A log rotated away between listing the directory and reading the file must
    not end the whole pass.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _is_within(candidate, parent):
    """Is candidate at or below parent, whatever either one is spelled like?

    BOTH sides are resolved. Resolving only the candidate compares a clean path
    against a raw one, and the ancestor guard below then passes anything spelled
    with a "..", a symlink or a Windows 8.3 short name: C:\\jobs\\x\\.. is the
    same directory as C:\\jobs and would not be recognised as one.
    """
    try:
        candidate.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def unique_directories(paths):
    """Existing directories, in order, with the same directory named once.

    Keyed on os.path.normcase of the resolved path, which folds case on Windows
    and leaves it alone everywhere else. A plain .lower() would be wrong on
    Linux, where Logs/ and logs/ are two real directories and lowercasing the
    key silently drops one of them.
    """
    result = []
    seen = set()
    for path in paths:
        if not path.is_dir():
            continue
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def discover_log_directories(script_path, working_directory=None):
    """A job's logs live beside its script, in a Logs subdirectory of the
    script's own folder, or -- when the scheduled task's action carries its own
    working directory -- beside that folder too. Task Scheduler lets an action's
    WorkingDirectory differ from where the script file lives, and without
    seeding from both roots those logs are invisible.
    """
    roots = []
    script_root = None
    clean = _clean_text(script_path)
    if clean:
        # Keep script_root even when the directory is absent on this machine:
        # the ancestor test below must be lexical, or it silently stops guarding
        # whenever the script path is not local (an off-host run, a relocated
        # script) -- exactly when bad data is most likely.
        script_root = Path(clean).parent
        roots.append(script_root)

    wd_clean = _clean_text(working_directory)
    if wd_clean:
        wd_root = Path(wd_clean)
        # Refuse a working directory that is an ANCESTOR of the script
        # directory. Measured on one host: 7 of 25 scheduled tasks declared the
        # shared repository root as their working directory, so accepting it
        # would mine the same root level log into 7 unrelated jobs and report
        # one job's record count as every job's. An ancestor also never finds
        # anything the script's own folder would not.
        if not (script_root and _is_within(script_root, wd_root)):
            roots.append(wd_root)

    candidates = []
    for root in roots:
        candidates.append(root)
        for name in LOG_DIR_NAMES:
            candidates.append(root / name)
    return unique_directories(candidates)


def recent_log_files(directories, days_back=DEFAULT_DAYS_BACK, limit=DEFAULT_MAX_FILES):
    """The newest log files across these directories, inside the day window."""
    cutoff = (_utc_now() - timedelta(days=days_back)).timestamp()
    found = []
    for directory in directories:
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            _emit("WARNING: could not list %s: %s" % (directory, exc))
            continue
        for path in entries:
            if not path.is_file() or path.suffix.lower() not in LOG_SUFFIXES:
                continue
            mtime = _mtime(path)
            if mtime is not None and mtime >= cutoff:
                found.append((mtime, path))
    found.sort(key=lambda pair: (-pair[0], str(pair[1])))
    return [path for _, path in found[:limit]]


def scan_log_file(path, max_lines=DEFAULT_MAX_LINES, rules=()):
    """The last value seen per metric in this file, plus line level counters.

    Last value wins within a file: a job that reports a running total reports
    the final one last.
    """
    metrics = {}
    lines_scanned = error_lines = warning_lines = 0

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        _emit("WARNING: could not read %s: %s" % (path, exc))
        return {"metrics": {}, "lines_scanned": 0, "error_lines": 0, "warning_lines": 0}

    with handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            lines_scanned += 1
            lower = line.lower()
            if ("traceback" in lower or " - error -" in lower
                    or " failed" in lower or lower.startswith("error")):
                error_lines += 1
            if "warning" in lower:
                warning_lines += 1

            for key, value in mine_line(line, rules):
                metrics[key] = (value, line[:240])

            if 0 < max_lines <= lines_scanned:
                break

    return {"metrics": metrics, "lines_scanned": lines_scanned,
            "error_lines": error_lines, "warning_lines": warning_lines}


def build_rows(paths, rules=(), max_lines=DEFAULT_MAX_LINES):
    """(metric rows, per file summaries) for these log files.

    One row per (file, metric). The file's modification time is the
    observation timestamp, because a scheduled job's log file is one run.
    """
    rows = []
    summaries = []
    for path in paths:
        mtime = _mtime(path)
        if mtime is None:
            _emit("WARNING: %s vanished during the scan; skipping it" % path)
            continue
        observed = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        scan = scan_log_file(path, max_lines, rules)
        for key in sorted(scan["metrics"]):
            value, sample = scan["metrics"][key]
            rows.append({
                "metric_key": key,
                "display_name": title_case_metric(key),
                "metric_value": value,
                "observed_at_utc": observed,
                "log_file": path.name,
                "sample_line": sample,
            })
        summaries.append({
            "log_file": path.name,
            "lines_scanned": scan["lines_scanned"],
            "error_lines": scan["error_lines"],
            "warning_lines": scan["warning_lines"],
            "metrics_found": len(scan["metrics"]),
        })
    rows.sort(key=lambda row: (row["metric_key"], row["observed_at_utc"], row["log_file"]))
    return rows, summaries


def write_text(path, text, apply_it):
    """Write the rendering to a file. Nothing is written without --apply.

    Bytes, not text mode: a CSV written in text mode on Windows carries CRLF
    and the same command on Linux carries LF, so the same logs would produce
    two different files.
    """
    data = text.encode("utf-8")
    if not apply_it:
        _emit("would write %d byte(s) to %s%s. Nothing written: add --apply."
              % (len(data), path, " (overwriting it)" if os.path.exists(path) else ""))
        return 0
    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        _emit("ERROR: could not write %s: %s" % (path, exc))
        return 2
    _emit("wrote %d byte(s) to %s" % (len(data), path))
    return 0


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core, then over the io layer.

    Nothing here reaches the network, a database or arcpy. The io half writes
    log fixtures into a temporary directory and mines them, because discovery,
    the day window and the refusal to write without --apply cannot be tested
    any other way.
    """
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def capture(fn):
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = fn()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, out.getvalue(), err.getvalue()

    def write(path, text):
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
        return path

    print("logsift self-test: no network, no database, no arcpy")
    print("-" * 68)

    # ---- numbers and keys
    check(parse_number("1,432") == 1432.0, "a thousands separator is read as one number")
    check(parse_number("-3.5") == -3.5, "a negative decimal is read")
    check(parse_number("") is None, "an empty value is not a number")
    check(parse_number("n/a") is None, "n/a is not a number")
    check(mine_line("Records updated: nan") == [] and mine_line("Duration: inf") == [],
          "nan and inf are words, not numbers: a NaN in a series charts as a hole")
    check(mine_line("Updated 999999999999999999999 records")
          == [("updated_record_count", 1e21)],
          "a count too big for an integer is still mined, at full magnitude")
    check(format_value(1e21) == "1e+21",
          "and prints in exponent form rather than as a rounded whole number")
    accented = canonicalize_metric_key("Parcels \xe9chou\xe9s count")
    check(accented == "parcels_chou_s_count",
          "a label with accents becomes an ascii key, so one series is not two spellings")
    check(normalize_metric_key("Records checked") == "records_checked",
          "a label becomes a lowercase underscored key")
    check(normalize_metric_key("Total records (all)") == "total_records_all",
          "punctuation collapses to single underscores")
    check(normalize_metric_key("   ") == "", "a blank label has no key")
    check(len(normalize_metric_key("a" * 200)) == 80, "a runaway label is cut at 80 characters")
    check(title_case_metric("updated_record_count") == "Updated Record Count",
          "a key gets a readable display name")

    # ---- the pinned defect: the log level is not part of the metric name
    for preamble in ("2026-08-11 03:14:07 - INFO - ",
                     "2026-08-11 03:14:07 | INFO | ",
                     "2026-08-11 03:14:07 INFO - ",
                     "[2026-08-11 03:14:07] ",
                     "2026-08-11 03:14:07 "):
        line = preamble + "Duration: 0:00:07"
        check(mine_line(line) == [("duration_seconds", 7.0)],
              "%r mines exactly one pair, seven seconds  <-- pinned defect"
              % preamble.strip())
        check(not [key for key, _ in extract_metrics_from_line(line) if key.startswith("info_")],
              "%r never mines an info_ key  <-- pinned defect" % preamble.strip())

    check(mine_line("2026-08-11 17:45:13,116 - INFO - Duration: 0:00:07.133764")
          == [("duration_seconds", 7.133764)],
          "a comma separated millisecond stamp is preamble, not a metric")
    check(mine_line("INFO - Records updated: 69") == [("updated_record_count", 69.0)],
          "a level with no timestamp is preamble too")
    check(mine_line("WARNING: Records checked: 12") == [("checked_record_count", 12.0)],
          "a level followed by a colon is preamble too")
    check(mine_line("2026-08-10 08:17:33 | INFO | update_parcels | Records checked: 47285")
          == [("checked_record_count", 47285.0)],
          "a logger's module field does not become part of the key")
    check(dict(extract_metrics_from_line(
        "2026-08-12 08:21:36,932 - INFO -   Verification: 15,562 features in hosted layer"
    )).get("verification") == 15562.0,
          "stripping the preamble does not swallow the payload  <-- pinned defect")

    # ---- the pinned defect: a number that is really a clock
    check(extract_metrics_from_line("Start time: 2026-08-11 03:14:07")
          == [("start_time", 2026.0)],
          "a start time really does mine the year as a number  <-- pinned defect")
    check(canonicalize_metric_key("start_time") is None,
          "and start_time is refused as a metric  <-- pinned defect")
    check(mine_line("2026-08-11 03:14:00 - INFO - Start time: 2026-08-11 03:14:07") == [],
          "so the line yields nothing at all  <-- pinned defect")
    check(mine_line("Finished at: 2026-08-11 03:14:07") == [],
          "a finish time is refused the same way")
    check(canonicalize_metric_key("time") is None, "a bare time is not a metric")
    check(canonicalize_metric_key("finished") is None, "finished is not a metric")
    check(canonicalize_metric_key("failed") is None, "failed is not a metric")
    check(canonicalize_metric_key("runtimeerror") is None, "runtimeerror is not a metric")
    check(canonicalize_metric_key("cpp") is None, "arcpy's cpp line is not a metric")
    check(canonicalize_metric_key("s_features") is None, "arcpy's s_features is not a metric")
    check(canonicalize_metric_key("pass_3_complete") is None, "a pass counter is not a metric")
    check(canonicalize_metric_key("starting_field_updates_batch_size") is None,
          "a batch size is a setting the job echoed, not a measurement")
    check(canonicalize_metric_key("a3f9c2b7e1d40852") is None,
          "a hex build id is not a metric")

    # ---- the pinned defect: one canonical key cannot be mined twice from one line
    collide = "Duration: 0:00:07 Elapsed time: 0:01:00"
    check(mine_line(collide) == [("duration_seconds", 7.0)],
          "a second duration on one line cannot overwrite the first with a zero  <-- pinned defect")
    raw = dict(extract_metrics_from_line(collide))
    check(raw.get("elapsed_time") == 0.0,
          "and the raw pass really does mine that zero, so the guard is load bearing  <-- pinned defect")
    check(canonicalize_metric_key("elapsed_time") == "duration_seconds",
          "because elapsed_time and duration are one series  <-- pinned defect")
    check(mine_line("Records updated: 5, updated 5 records")
          == [("updated_record_count", 5.0)],
          "the label form and the sentence form of one metric mine once")

    # The raw pass has its own guard, and mine_line's canonical guard hides it:
    # both layers have to be asserted separately or only the outer one is really
    # tested. Removing any of the three raw guards below leaves every mine_line
    # assertion in this file passing.
    check(extract_metrics_from_line("Duration: 0:00:07") == [("duration_seconds", 7.0)],
          "the clock matcher reserves its own label, so 'duration' is not also "
          "mined as zero  <-- pinned defect")
    check(extract_metrics_from_line("Records updated: 5, Records updated: 9")
          == [("records_updated", 5.0)],
          "a label repeated on one line is mined once, at its first value")
    check(extract_metrics_from_line("Updated 5 records, updated 7 records")
          == [("updated_records", 5.0)],
          "and a verb phrase repeated on one line is mined once too")

    # ---- durations
    check(mine_line("Total time: 1:02:30") == [("duration_seconds", 3750.0)],
          "an hour, two minutes and thirty seconds is 3750 seconds")
    check(mine_line("Duration: 02:30") == [("duration_seconds", 150.0)],
          "a clock with no hours field is minutes and seconds")
    check(mine_line("2026-08-11 03:14:07 - INFO - Duration: 0:00:7")
          == [("duration_seconds", 7.0)],
          "a hand formatted clock with a one digit seconds field is still a clock, "
          "not the zero of its hours  <-- pinned defect")
    check(mine_line("Duration: 0:1:7") == [("duration_seconds", 67.0)],
          "and one digit in the minutes field reads the same way")
    check(mine_line("INFO - Total execution time: 970.13 seconds")
          == [("duration_seconds", 970.13)],
          "a duration in plain seconds lands on the same key")
    for label in ("duration", "elapsed_time", "processing_time", "total_time",
                  "total_execution_time", "run_time"):
        check(canonicalize_metric_key(label) == "duration_seconds",
              "%s is one series with the others" % label)

    # ---- counts
    check(mine_line("Updated 1432 records") == [("updated_record_count", 1432.0)],
          "a sentence count is mined")
    check(mine_line("Records updated: 1,432") == [("updated_record_count", 1432.0)],
          "and the label form gives the same key and the same number")
    check(mine_line("Updated 1432 rows") == [("updated_record_count", 1432.0)],
          "rows and records are one series, not two")
    check(mine_line("Loaded 47285 source records") == [("source_record_count", 47285.0)],
          "a source count is mined")
    check(mine_line("Deleted 0 features") == [("deleted_record_count", 0.0)],
          "a real zero is mined: only a zero that is a clock is refused")
    check(mine_line("Processed 12 rows") == [("processed_record_count", 12.0)],
          "a processed count is mined")
    check(mine_line("Exported 7 features") == [("exported_record_count", 7.0)],
          "an exported count is mined")
    check(mine_line("Inserted 9 records") == [("inserted_record_count", 9.0)],
          "an inserted count is mined")
    check(canonicalize_metric_key("updated") == "updated_record_count",
          "a bare verb key joins its own series rather than starting a new one")
    check(mine_line("Updated 5") == [("updated_record_count", 5.0)],
          "a count with a verb and no noun after it is still that series")
    check(mine_line("Updated 5 records, updated 7 records")
          == [("updated_record_count", 5.0)],
          "a number repeated on one line cannot start a second series")
    check(mine_line("Updated 1432 records in the parcels layer")
          == [("updated_records_in_the_parcels_layer", 1432.0)],
          "a sentence count takes its whole tail as the key: give it a rule to shorten it")

    # ---- the vocabulary filter
    check(canonicalize_metric_key("widgets") is None, "widgets is not a word this tool charts")
    check(canonicalize_metric_key("some_random_label") is None,
          "a label with no measuring word in it is refused")
    check(canonicalize_metric_key("!!!") is None,
          "a label with no letter or digit in it has no key at all")
    # A supplied rule wins over everything, so without the empty-key guard the
    # pattern "" -- which any regex engine matches against an empty string --
    # invents a metric out of a label that has no key at all.
    check(canonicalize_metric_key("!!!", load_rules('{"": "ghost_count"}')) is None,
          "and no supplied rule can invent a metric from it  <-- pinned defect")
    check(canonicalize_metric_key("error_count") == "error_count",
          "a metric really named error_count is not cut down to count  <-- pinned defect")
    check(mine_line("Widgets: 12") == [], "so the line mines nothing")
    check(canonicalize_metric_key("new_parcel_count") == "new_parcel_count",
          "an unknown but plainly countable key is kept as it is")

    # ---- a rules file
    rules = load_rules('{"^verification$": "verified_record_count"}')
    check(len(rules) == 1, "a one rule document compiles to one rule")
    check(mine_line("Verification: 15,562 features in hosted layer", rules)
          == [("verified_record_count", 15562.0)],
          "a supplied rule mines a label the vocabulary filter would refuse")
    beats = load_rules('{"^duration$": "duration_ms"}')
    check(canonicalize_metric_key("duration", beats) == "duration_ms",
          "a supplied rule beats a built-in one  <-- pinned defect")
    check(canonicalize_metric_key("elapsed_time", beats) == "duration_seconds",
          "and leaves the built-in rules it does not match alone")
    check(canonicalize_metric_key("updated_count", load_rules('{"^updated_count$": null}')) is None,
          "a rule mapped to null drops that label as noise")
    check(canonicalize_metric_key("records_checked",
                                  load_rules('{"^checked_record_count$": null}')) is None,
          "a rule written against the canonical key reaches the label too  <-- pinned defect")
    check(canonicalize_metric_key("records_checked",
                                  load_rules('{"^checked_record_count$": "checked_rows"}'))
          == "checked_rows",
          "and can rename a series the built-in rules created")
    check(canonicalize_metric_key("x_count", load_rules('{"^x_count$": "My Widgets"}'))
          == "my_widgets",
          "a supplied canonical key is normalized like any other key")
    raises(lambda: load_rules("{not json"), "a rules file that is not JSON is a usage error")
    raises(lambda: load_rules('["^a$", "b"]'), "a JSON list is not a rules document")
    raises(lambda: load_rules('{"^a$": 5}'), "a numeric canonical key is refused")
    raises(lambda: load_rules('{"^a$": "  "}'), "an empty canonical key is refused")
    raises(lambda: load_rules('{"^(a$": "b_count"}'), "an unparseable pattern is refused")

    # ---- rendering
    sample = [{"metric_key": "duration_seconds", "display_name": "Duration Seconds",
               "metric_value": 7.133764, "observed_at_utc": "2026-08-11T03:14:07+00:00",
               "log_file": "nightly.log", "sample_line": "Duration: 0:00:07, ok"},
              {"metric_key": "updated_record_count", "display_name": "Updated Record Count",
               "metric_value": 1432.0, "observed_at_utc": "2026-08-11T03:14:07+00:00",
               "log_file": "nightly.log", "sample_line": "Records updated: 1,432"}]
    check(format_value(1432.0) == "1432", "a whole number prints without a decimal point")
    check(format_value(7.133764) == "7.133764", "a fraction keeps every digit it was given")
    check(format_value(0.0) == "0", "zero prints as zero")
    table = render(sample, "table")
    check("duration_seconds" in table and "7.133764" in table,
          "the table carries the key and the value")
    check(table.splitlines()[1].startswith("-"), "the table has a dashed rule under its header")
    check(len(render([], "table").splitlines()) == 2,
          "an empty result still prints a header, not nothing at all")
    body = render(sample, "csv")
    check(body.splitlines()[0] == ",".join(ROW_FIELDS), "the csv names its columns")
    check("\r" not in body,
          "the csv is LF only, so Windows and Linux write the same bytes  <-- pinned defect")
    check('"Duration: 0:00:07, ok"' in body, "a sample line with a comma is quoted")
    check(len(list(csv.reader(io.StringIO(body)))) == 3, "the csv parses back to header plus two rows")
    check(json.loads(render(sample, "json")) == sample, "the json round-trips every field")
    check(",1432," in body, "the csv carries 1432, not 1432.0")

    # ---- the io layer, in a temporary directory
    tmp = tempfile.mkdtemp(prefix="logsift_selftest_")
    here = os.getcwd()
    try:
        # ---- discovery
        project = os.path.join(tmp, "proj")
        script = os.path.join(project, "nightly.py")
        write(script, "")
        write(os.path.join(project, "Logs", "nightly_0811.log"),
              "2026-08-11 03:14:00 - INFO - Start time: 2026-08-11 03:14:00\n"
              "2026-08-11 03:14:03 - INFO - Records updated: 1,432\n"
              "2026-08-11 03:14:07 - INFO - Duration: 0:00:07.133764\n")
        found = discover_log_directories(script)
        check(any(path.name == "Logs" for path in found),
              "the Logs folder beside the script is found")
        check(len([path for path in found if path.name.lower() == "logs"]) == 1,
              "Logs, logs, Log and log resolve to one candidate  <-- pinned defect")
        check(len(found) == 2, "the script's own folder is searched as well")
        check(discover_log_directories(os.path.join(tmp, "nowhere", "x.py")) == [],
              "a script path that is not on this machine finds nothing")
        check(discover_log_directories("") == [], "no script and no working directory finds nothing")

        sidecar = os.path.join(tmp, "sidecar")
        write(os.path.join(sidecar, "Logs", "run.log"), "Records updated: 55\n")
        found = discover_log_directories(script, sidecar)
        check(any("sidecar" in str(path) and path.name == "Logs" for path in found),
              "a working directory beside the script contributes its own Logs folder")
        check(len(discover_log_directories(os.path.join(tmp, "nowhere", "x.py"), sidecar)) == 2,
              "a working directory is used even when the script is not on this machine")

        # A job whose folder is spelled logs, not Logs. Windows cannot tell the
        # two apart, so this assertion only has teeth on Linux -- which is
        # exactly where dropping a spelling from LOG_DIR_NAMES loses the logs.
        lower = os.path.join(tmp, "lower")
        write(os.path.join(lower, "nightly.py"), "")
        write(os.path.join(lower, "logs", "run.log"), "Records updated: 77\n")
        found = discover_log_directories(os.path.join(lower, "nightly.py"))
        check(any(path.name.lower() == "logs" for path in found),
              "a lowercase logs folder is found, not only a capitalised one")

        # ---- the pinned defect: an ancestor working directory
        write(os.path.join(tmp, "Logs", "shared.log"), "Records updated: 999\n")
        found = discover_log_directories(script, tmp)
        resolved = set(os.path.normcase(str(path.resolve())) for path in found)
        check(os.path.normcase(str(Path(tmp).resolve())) not in resolved,
              "an ancestor working directory is refused  <-- pinned defect")
        check(os.path.normcase(str((Path(tmp) / "Logs").resolve())) not in resolved,
              "so one shared log is not charted as every job's own  <-- pinned defect")
        # The same ancestor, spelled with a "..". Resolving only the candidate
        # and not the parent lets this one straight through the guard.
        dotdot = discover_log_directories(script, os.path.join(tmp, "proj", ".."))
        dotdot_resolved = set(os.path.normcase(str(path.resolve())) for path in dotdot)
        check(os.path.normcase(str((Path(tmp) / "Logs").resolve())) not in dotdot_resolved,
              "an ancestor spelled with a .. is refused too  <-- pinned defect")
        # A script that is not on this machine at all. The guard has to be
        # lexical: if it needs the script's folder to exist, it stops guarding
        # for every off-host run, which is when bad data is most likely.
        check(discover_log_directories(os.path.join(tmp, "nowhere", "deep", "x.py"), tmp)
              == [],
              "an ancestor is refused even when the script's folder is not local "
              " <-- pinned defect")

        # ---- case folding is the filesystem's business, not the string's
        case_root = os.path.join(tmp, "case")
        os.makedirs(os.path.join(case_root, "Logs"))
        try:
            os.makedirs(os.path.join(case_root, "LOGS"))
        except OSError:
            pass
        folded = unique_directories([Path(case_root) / "Logs", Path(case_root) / "LOGS"])
        if os.path.normcase("A") == "a":
            check(len(folded) == 1,
                  "Logs and LOGS are ONE directory where the filesystem folds case  <-- pinned defect")
        else:
            check(len(folded) == 2,
                  "Logs and LOGS are TWO directories where it does not  <-- pinned defect")
        check(len(unique_directories([Path(project), Path(project) / "." ,
                                      Path(project) / "Logs" / ".."])) == 1,
              "three spellings of one directory are scanned once")
        ordered = discover_log_directories(script)
        check([path.name for path in ordered] == ["proj", "Logs"],
              "the script's own folder is offered before its Logs folder, in that order")
        check(unique_directories([Path(os.path.join(tmp, "nowhere"))]) == [],
              "a directory that does not exist is not a candidate")

        # ---- the day window and the file limit
        logdir = Path(project) / "Logs"
        old = write(os.path.join(project, "Logs", "nightly_0501.log"), "Records updated: 1\n")
        os.utime(old, (1000000000, 1000000000))
        files = recent_log_files([logdir], days_back=30)
        check(len(files) == 1 and files[0].name == "nightly_0811.log",
              "a log older than the window is left out")
        check(len(recent_log_files([logdir], days_back=36500)) == 2,
              "and is mined when the window is wide enough to reach it")
        check(len(recent_log_files([logdir], days_back=36500, limit=1)) == 1,
              "the file limit keeps the newest and stops")
        write(os.path.join(project, "Logs", "notes.csv"), "a,b\n1,2\n")
        check(len(recent_log_files([logdir], days_back=36500)) == 2,
              "a csv beside the logs is data, not a log")
        listed, _, warned = capture(
            lambda: recent_log_files([Path(os.path.join(tmp, "nowhere"))]))
        check(listed == [], "a directory that cannot be listed is a warning, not a crash")
        check("could not list" in warned, "and the warning names the directory")
        check(_mtime(Path(os.path.join(tmp, "nowhere", "gone.log"))) is None,
              "a file that is not there has no timestamp")

        # ---- scanning one file
        scan = scan_log_file(logdir / "nightly_0811.log")
        check(scan["lines_scanned"] == 3, "every non blank line is counted")
        check(scan["metrics"]["duration_seconds"][0] == 7.133764,
              "the real duration survives the whole pipeline  <-- pinned defect")
        check(scan["metrics"]["updated_record_count"][0] == 1432.0,
              "and so does the record count")
        check("start_time" not in scan["metrics"], "and the start time never enters the file")
        check(len(scan["metrics"]) == 2, "three lines yield exactly two metrics")
        running = write(os.path.join(tmp, "running.log"),
                        "Records updated: 10\n\nRecords updated: 20\nRecords updated: 30\n")
        check(scan_log_file(Path(running))["metrics"]["updated_record_count"][0] == 30.0,
              "the last value in a file wins, because a running total ends at the total")
        check(scan_log_file(Path(running))["lines_scanned"] == 3,
              "a blank line is not a line that was scanned")
        check(scan_log_file(Path(running), max_lines=2)["lines_scanned"] == 2,
              "the line limit stops the read early")
        check(scan_log_file(Path(running), max_lines=2)["metrics"]["updated_record_count"][0] == 20.0,
              "and the metric reflects only the lines that were read")
        # --max-lines 0 means no limit, which is what the README promises. Drop
        # the "0 <" from the limit test and 0 stops the read at the first line
        # instead: the file is read, nothing is mined, and the run reports
        # success with an empty series.
        check(scan_log_file(Path(running), max_lines=0)["lines_scanned"] == 3,
              "--max-lines 0 reads the whole file rather than none of it")
        noisy = write(os.path.join(tmp, "noisy.log"),
                      "2026-08-11 03:14:00 - ERROR - job failed\n"
                      "Traceback (most recent call last):\n"
                      "2026-08-11 03:14:01 - WARNING - retrying\n")
        noisy_scan = scan_log_file(Path(noisy))
        check(noisy_scan["error_lines"] == 2, "an error line and a traceback line are counted")
        check(noisy_scan["warning_lines"] == 1, "warning lines are counted separately")
        check(noisy_scan["metrics"] == {}, "and a traceback mines no metrics at all")
        unreadable, _, warned = capture(lambda: scan_log_file(logdir))
        check(unreadable["lines_scanned"] == 0 and unreadable["metrics"] == {},
              "a path that cannot be opened is a warning, not a crash")
        check("could not read" in warned, "and the warning names the file")
        binary = os.path.join(tmp, "binary.log")
        with open(binary, "wb") as handle:
            handle.write(b"Records updated: 5\n\xff\xfe not utf-8 at all\n")
        check(scan_log_file(Path(binary))["metrics"]["updated_record_count"][0] == 5.0,
              "bytes that are not valid utf-8 are replaced, not fatal")

        # ---- rows
        rows, summaries = build_rows([logdir / "nightly_0811.log"])
        check(len(rows) == 2 and rows[0]["metric_key"] == "duration_seconds",
              "one row per metric per file, sorted by key")
        check(rows[0]["display_name"] == "Duration Seconds", "each row carries a display name")
        check(rows[0]["observed_at_utc"].endswith("+00:00"),
              "the observation time is the file's own mtime, in UTC")
        check(rows[0]["sample_line"].endswith("Duration: 0:00:07.133764"),
              "and the line the number came from, so a wrong number is traceable")
        check(summaries[0]["metrics_found"] == 2, "the per file summary counts the metrics")
        (gone, gone_summaries), _, warned = capture(
            lambda: build_rows([Path(os.path.join(tmp, "nowhere", "gone.log"))]))
        check(gone == [], "a log that vanished mid pass is skipped, not fatal")
        check("vanished" in warned, "and says which one it lost")
        # Rows alone do not prove it was skipped: a file that is no longer there
        # yields no rows either way. The summary is what the run counts reports.
        check(gone_summaries == [],
              "and it is not counted as a log file that was read  <-- pinned defect")

        # ---- argument parsing
        args = _parse([])
        check(args.apply is False, "--apply is OFF by default  <-- pinned defect")
        check(args.out is None, "no output file by default")
        check(args.format == "table", "the table is the default format")
        # The literals, not the constants. Comparing args.days against
        # DEFAULT_DAYS_BACK passes whatever DEFAULT_DAYS_BACK is changed to,
        # which is no assertion at all: the README and --help both say 30.
        check(args.days == 30, "the window defaults to 30 days")
        check(args.max_files == 40, "at most 40 files per run by default")
        check(args.max_lines == 5000, "at most 5000 lines per file by default")
        check(args.paths == [] and args.script is None and args.rules is None,
              "and nothing is read until you say what to read")
        args = _parse(["a", "b", "--script", "s.py", "--working-directory", "w",
                       "--days", "7", "--max-files", "3", "--max-lines", "9",
                       "--rules", "r.json", "--format", "json", "--out", "o.json",
                       "--apply", "--self-test"])
        check(args.paths == ["a", "b"] and args.script == "s.py"
              and args.working_directory == "w" and args.days == 7
              and args.max_files == 3 and args.max_lines == 9
              and args.rules == "r.json" and args.format == "json"
              and args.out == "o.json" and args.apply is True and args.self_test is True,
              "every flag is read, none of them silently ignored")
        try:
            code = None
            _, _, _ = capture(lambda: _parse(["--format", "yaml"]))
        except SystemExit as exc:
            code = exc.code
        check(code == 2, "an unknown format is a usage error, not a crash")
        try:
            code = None
            _, _, _ = capture(lambda: _parse(["--ap"]))
        except SystemExit as exc:
            code = exc.code
        check(code == 2, "a unique prefix of --apply is refused, not read as --apply  <-- pinned defect")

        # ---- main, end to end
        rc, out, err = capture(lambda: main([]))
        check(rc == 64 and "pass a log file" in err,
              "no arguments is a usage error that says what to pass")
        rc, out, err = capture(lambda: main([os.path.join(tmp, "nowhere")]))
        check(rc == 64 and "no such file or directory" in err,
              "a path that does not exist is a usage error naming the path")
        # With a real directory alongside it the run has something to mine, so
        # exit 64 here can only come from the missing path itself. Without this
        # case a typo'd second path is mined away silently and the chart it was
        # meant to feed stays empty for as long as nobody looks.
        rc, out, err = capture(lambda: main([str(logdir), os.path.join(tmp, "nowhere")]))
        check(rc == 64 and out == "",
              "one bad path among good ones fails the whole run  <-- pinned defect")
        rc, out, err = capture(lambda: main([str(logdir), "--rules", os.path.join(tmp, "none.json")]))
        check(rc == 64 and "rules" in err, "a rules file that is not there is a usage error")
        bad = write(os.path.join(tmp, "bad.json"), "{not json")
        rc, out, err = capture(lambda: main([str(logdir), "--rules", bad]))
        check(rc == 64 and "JSON" in err, "an unreadable rules file is a usage error")

        rc, out, err = capture(lambda: main([str(logdir)]))
        check(rc == 0, "a directory of logs mines something")
        check("duration_seconds" in out and "7.133764" in out, "and prints the series")
        check("start_time" not in out, "and no start time reached the table  <-- pinned defect")
        check("nightly_0501.log" not in out, "and the log outside the window was not read")

        rc, out, err = capture(lambda: main([str(logdir), "--format", "csv"]))
        parsed = list(csv.reader(io.StringIO(out)))
        check(rc == 0 and parsed[0] == list(ROW_FIELDS),
              "stdout carries ONLY the data, so a redirect gives a clean csv  <-- pinned defect")
        check(len(parsed) == 3, "two metric rows and a header")
        check("log file(s)" in err, "and the narrative went to stderr")

        rc, out, err = capture(lambda: main([str(logdir), "--format", "json"]))
        check(rc == 0 and len(json.loads(out)) == 2, "json is parseable on its own")

        rc, out, err = capture(lambda: main([str(logdir), "--days", "36500"]))
        check(rc == 0 and "nightly_0501.log" in out, "a wider window reaches the older log")
        rc, out, err = capture(lambda: main([str(logdir), "--days", "36500", "--max-files", "1"]))
        check(rc == 0 and "nightly_0501.log" not in out, "--max-files keeps the newest only")
        rc, out, err = capture(lambda: main([str(logdir), "--max-lines", "0"]))
        check(rc == 0 and "7.133764" in out,
              "--max-lines 0 reads to the end of the file, it does not read nothing")
        rc, out, err = capture(lambda: main([str(logdir / "nightly_0811.log")]))
        check(rc == 0 and "duration_seconds" in out, "a log file can be named directly")

        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        rc, out, err = capture(lambda: main([empty]))
        check(rc == 1 and "no log file" in err,
              "a folder with no recent log is exit 1 and says which folder it read")
        quiet = write(os.path.join(tmp, "quiet", "run.log"),
                      "2026-08-11 03:14:00 - INFO - Start time: 2026-08-11 03:14:00\n"
                      "2026-08-11 03:14:07 - INFO - Widgets: 12\n")
        rc, out, err = capture(lambda: main([os.path.dirname(quiet)]))
        check(rc == 1, "a log whose every number is refused is exit 1, not a silent success")
        check("1 log file(s)" in err, "and it says it really did read the file")

        rc, out, err = capture(lambda: main(["--script", script]))
        check(rc == 0 and "updated_record_count" in out, "--script finds the Logs folder itself")
        # A scheduled action's path copied out of Task Scheduler often carries
        # a trailing space. On Linux that space is part of the directory name
        # and the job's logs simply are not there.
        rc, out, err = capture(lambda: main(["--script", "  " + script + "  "]))
        check(rc == 0 and "updated_record_count" in out,
              "a script path padded with whitespace still finds its logs")
        rc, out, err = capture(lambda: main(["--script", os.path.join(tmp, "nowhere", "x.py"),
                                             "--working-directory", sidecar]))
        check(rc == 0 and "55" in out, "--working-directory mines the sidecar logs")
        rc, out, err = capture(lambda: main(["--script", script, "--working-directory", tmp]))
        check(rc == 0 and "999" not in out,
              "and an ancestor working directory contributes nothing  <-- pinned defect")

        rules_file = write(os.path.join(tmp, "rules.json"),
                           '{"^verification$": "verified_record_count",\n'
                           ' "^updated_%s$": null}' % "records?")
        veri = write(os.path.join(tmp, "veri", "run.log"),
                     "2026-08-12 08:21:36,932 - INFO - Verification: 15,562 features\n"
                     "2026-08-12 08:21:37,001 - INFO - Updated records: 41\n")
        rc, out, err = capture(lambda: main([os.path.dirname(veri), "--rules", rules_file]))
        check(rc == 0 and "verified_record_count" in out,
              "a supplied rule mines a metric the built-in rules refuse")
        check("updated_record_count" not in out,
              "and a rule mapped to null drops one the built-in rules would keep")

        # ---- the pinned defect: nothing is written without --apply
        target = os.path.join(tmp, "metrics.csv")
        rc, dry_out, err = capture(lambda: main([str(logdir), "--format", "csv", "--out", target]))
        check(rc == 0 and not os.path.exists(target),
              "--out without --apply writes NOTHING  <-- pinned defect")
        check("Nothing written" in err and "--apply" in err, "and says how to write it")
        rc, out, err = capture(lambda: main([str(logdir), "--format", "csv",
                                             "--out", target, "--apply"]))
        check(rc == 0 and os.path.isfile(target), "--apply writes the file")
        check(out == "",
              "and writes it INSTEAD of stdout, not as well as  <-- pinned defect")
        with open(target, "rb") as handle:
            written = handle.read()
        check(written.decode("utf-8") == dry_out,
              "and writes exactly what the dry run printed  <-- pinned defect")
        check(b"\r" not in written,
              "the written csv is LF only on every platform  <-- pinned defect")
        rc, out, err = capture(lambda: main([str(logdir), "--format", "csv",
                                             "--out", target]))
        check("overwriting" in err, "a second dry run says the file would be overwritten")
        with open(target, "rb") as handle:
            check(handle.read() == written, "and leaves the existing file untouched  <-- pinned defect")
        rc, out, err = capture(lambda: main([str(logdir), "--out",
                                             os.path.join(tmp, "nowhere", "x.csv"), "--apply"]))
        check(rc == 2 and "could not write" in err,
              "a write that fails is exit 2, not a false success")

        # ---- the harness itself. A check() that cannot record a failure would
        # report every defect above as a pass, which is the one failure no other
        # assertion here could see. Three deliberate failures are recorded
        # against a scratch mark and then taken back off the tally.
        loud = sys.stdout
        sys.stdout = io.StringIO()
        mark = len(failed)
        passed_mark = passed[0]
        try:
            check(False, "probe: a false condition must be recorded as a failure")
            raises(lambda: None, "probe: a call that raises nothing must fail")
            raises(lambda: [][0], "probe: the wrong exception must fail")
        finally:
            sys.stdout = loud
        probe = failed[mark:]
        probe_passed = passed[0] - passed_mark
        del failed[mark:]
        # This verdict is NOT reported through check(). Routing it through the
        # mechanism it is testing makes it worthless: a check() that recorded a
        # failure as a pass would report this very assertion as a pass too, and
        # the run would end green with every defect above unreported.
        if len(probe) != 3 or probe_passed != 0:
            sys.stdout.write(
                "FATAL  check() and raises() did not record three deliberate "
                "failures (%d recorded, %d counted as passes). This run proves "
                "nothing.\n" % (len(probe), probe_passed))
            return 1
        passed[0] += 1
        print("PASS  check() and raises() really do record a failure  <-- pinned defect")
    finally:
        os.chdir(here)
        shutil.rmtree(tmp, ignore_errors=True)
    check(not os.path.isdir(tmp), "the self-test leaves no temporary directory behind")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="logsift.py",
        allow_abbrev=False,
        description="Mine a job's own log files into a metric series, and "
                    "refuse the number that is really a timestamp.",
        epilog="Data goes to stdout and messages go to stderr. Nothing is "
               "written to a file without --apply.",
    )
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="log files, or directories of them, to mine")
    parser.add_argument("--script", metavar="PATH",
                        help="a scheduled job's script. Its own folder and any "
                             "Logs folder beside it are searched.")
    parser.add_argument("--working-directory", dest="working_directory", metavar="DIR",
                        help="the scheduled action's working directory, when it "
                             "differs from the script's folder. An ancestor of "
                             "the script's folder is refused.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS_BACK, metavar="N",
                        help="only log files modified in the last N days (default 30)")
    parser.add_argument("--max-files", dest="max_files", type=int,
                        default=DEFAULT_MAX_FILES, metavar="N",
                        help="at most N log files, newest first (default 40)")
    parser.add_argument("--max-lines", dest="max_lines", type=int,
                        default=DEFAULT_MAX_LINES, metavar="N",
                        help="at most N lines per file, 0 for no limit (default 5000)")
    parser.add_argument("--rules", metavar="FILE",
                        help="a JSON object of {\"pattern\": \"canonical_key\"}. "
                             "Checked before the built-in rules. null drops the label.")
    parser.add_argument("--format", choices=FORMATS, default="table",
                        help="table, csv or json (default table)")
    parser.add_argument("--out", metavar="FILE",
                        help="write the rendering here instead of stdout. Needs --apply.")
    parser.add_argument("--apply", action="store_true",
                        help="write the --out file. Without this nothing is written.")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the offline assertions and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    rules = []
    if args.rules:
        try:
            with open(args.rules, "r", encoding="utf-8") as handle:
                rules = load_rules(handle.read())
        except OSError as exc:
            _emit("error: could not read the rules file %s: %s" % (args.rules, exc))
            return 64
        except ValueError as exc:
            _emit("error: the rules file %s is unusable: %s" % (args.rules, exc))
            return 64

    # A path that is not there is a typo or a folder somebody renamed. Treating
    # it as an empty directory would leave a scheduled chart reporting nothing
    # for as long as it takes anybody to notice.
    missing = [path for path in args.paths if not os.path.exists(path)]
    if missing:
        _emit("error: no such file or directory: %s" % ", ".join(missing))
        return 64

    named_files = [Path(path) for path in args.paths if os.path.isfile(path)]
    directories = [Path(path) for path in args.paths if os.path.isdir(path)]
    if args.script or args.working_directory:
        directories.extend(discover_log_directories(args.script, args.working_directory))
    directories = unique_directories(directories)

    if not directories and not named_files:
        _emit("error: pass a log file or a directory of them, or --script. "
              "Use --self-test to check the tool without any logs.")
        return 64

    # A file named on the command line is read whatever its age: naming it is
    # the instruction. The day window applies to what discovery found.
    files = named_files + recent_log_files(directories, args.days, args.max_files)
    if not files:
        _emit("no log file in the last %d day(s) under: %s"
              % (args.days, ", ".join(str(directory) for directory in directories)))
        return 1

    rows, summaries = build_rows(files, rules, args.max_lines)
    text = render(rows, args.format)

    written = 0
    if args.out:
        written = write_text(args.out, text, args.apply)
        if written:
            return written
        if args.apply:
            text = None

    if text is not None:
        sys.stdout.write(text)

    _emit("logsift: %d log file(s), %d line(s), %d metric row(s), %d error line(s)"
          % (len(summaries),
             sum(summary["lines_scanned"] for summary in summaries),
             len(rows),
             sum(summary["error_lines"] for summary in summaries)))
    if not rows:
        _emit("no number in those logs survived the rules. Run with --rules to "
              "name the labels this job uses.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
