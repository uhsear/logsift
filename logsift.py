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
    python logsift.py logs --tail --scheduled-at 03:00
    python logsift.py --self-test

Data goes to stdout, every message goes to stderr, so a redirect gives a clean
file. Exit codes: 0 metrics mined, 1 nothing to chart (no recent log file, or
no number in them survived the rules), 2 the write step failed, 64 usage error.
"""

from __future__ import print_function

import argparse
import codecs
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

# A log is sniffed from its first bytes, never assumed to be UTF-8. Windows
# PowerShell 5.1 writes Out-File, ">" and ">>" as UTF-16LE with a BOM, and
# Start-Transcript as UTF-8 with a BOM. Read as plain UTF-8, the first file is a
# NUL between every letter and mines nothing. The second carries U+FEFF in front
# of line one, so the preamble below no longer matches at position 0 and
# "INFO - Records updated" becomes the key info_records_updated again.
# Longest BOM first: the UTF-32LE BOM begins with the UTF-16LE one.
BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF32_LE, "utf-32-le", 4),
    (codecs.BOM_UTF32_BE, "utf-32-be", 4),
    (codecs.BOM_UTF8, "utf-8", 1),
    (codecs.BOM_UTF16_LE, "utf-16-le", 2),
    (codecs.BOM_UTF16_BE, "utf-16-be", 2),
)
SNIFF_BYTES = 8192

# --tail reads backwards in blocks of this size, so the last lines of a 2 GB
# log cost a few blocks, not the whole file.
TAIL_BLOCK_BYTES = 65536

# --scheduled-at: a run whose first timestamp is further than this from every
# scheduled time is a manual run. Python startup, an arcpy import and a queued
# task all delay the first log line, so this is minutes, not seconds.
DEFAULT_SLACK_MINUTES = 30

# The first timestamp in a log is when the run started. Python logging writes
# asctime as "2003-07-08 16:49:45,896", in local time, by default.
START_STAMP_RE = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[ T](\d{2}):(\d{2}):\d{2}")

# --sections: a banner line names the section the lines under it belong to.
# "=== Roads ===", "### Parcels ###", "----- Step 2: Roads -----". The same
# fence character, three or more times, on BOTH sides: a one sided
# "--- retrying" is a message, not a banner. The optional preamble is spelled
# out here, not borrowed from LOG_PREAMBLE_RE, because that one eats the first
# "-" of a fence.
SECTION_RE = re.compile(
    r"^\s*(?:\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\]?\s*(?:[-|:]\s+)?)?"
    r"(?:\[?(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\]?\s*(?:[-|:]\s+)?)?"
    r"(?P<fence>[=#*~-])(?P=fence){2,}\s*(?P<name>[^=#*~\s].*?)\s*(?P=fence){3,}\s*$"
)
SECTION_NAME_MAX = 40

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
# something it measured. A key ending in a singular "version" names one
# software version, and the Start-Transcript header writes nine of them
# ("PSVersion: 5.1.26100", "BuildVersion: 10.0...") that would chart as 5.1 and
# 10. A plural count such as reconciled_versions is kept. The last pattern is a
# build or commit id.
NOISE_METRIC_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^start_time$", r"^time$", r"^finished$", r"^failed$", r"^runtimeerror$",
        r"^cpp$", r"^s_features$", r"^pass_\d+_complete$",
        r"^.*batch_size$",
        r"^.*version$", r"^pscompatibleversions$",
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


def sniff_encoding(sample):
    """(codec, BOM length, code unit width) for a log's first bytes.

    A BOM decides. Without one, ASCII text in UTF-16 has a zero in every other
    byte, and the half that holds the zeros gives the byte order. Anything else
    is UTF-8, read with replacement, as before.
    """
    for bom, codec, width in BYTE_ORDER_MARKS:
        if sample.startswith(bom):
            return codec, len(bom), width
    pairs = len(sample) // 2
    if pairs:
        even = sample[0:pairs * 2:2].count(0)
        odd = sample[1:pairs * 2:2].count(0)
        # Zeros in both halves is a NUL padded file, not UTF-16.
        if odd * 2 > pairs and even * 10 < pairs:
            return "utf-16-le", 0, 2
        if even * 2 > pairs and odd * 10 < pairs:
            return "utf-16-be", 0, 2
    return "utf-8", 0, 1


def split_lines(text):
    """Lines as universal newline mode reads them: CRLF, LF and CR each end one."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def tail_lines(text, want, whole):
    """The last `want` non-blank lines of text, or all of them when want is 0.

    When the text does not begin at the start of the file, its first line is a
    fragment of a longer one and is dropped: half a line can mine half a number.
    """
    lines = split_lines(text)
    if not whole:
        lines = lines[1:]
    lines = [line for line in lines if line.strip()]
    return lines[-want:] if want > 0 else lines


def start_minutes(lines):
    """Minutes after midnight of the first timestamp in these lines, or None.

    Only the FIRST timestamp counts. A later one is a time the run reached, not
    the time it started, so an impossible first stamp is None, not a reason to
    look further down.
    """
    for line in lines:
        match = START_STAMP_RE.match(line)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour < 24 and minute < 60:
                return hour * 60 + minute
            return None
    return None


def parse_clock(text):
    """"03:14" as minutes after midnight. ValueError for anything else."""
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError("%r is not a time of day as HH:MM" % (text,))
    return int(match.group(1)) * 60 + int(match.group(2))


def clock(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def minutes_off_schedule(started, scheduled):
    """Minutes from a start time to the nearest scheduled time, round the clock.

    Round the clock, because a task scheduled at 23:50 that starts at 00:05 is
    15 minutes late, not 1425 minutes early.
    """
    gaps = [abs(started - at) % 1440 for at in scheduled]
    return min(min(gap, 1440 - gap) for gap in gaps)


def section_name(line):
    """The section a banner line opens, as a key, or None if it is no banner."""
    match = SECTION_RE.match(line)
    if not match or not re.search(r"[A-Za-z]", match.group("name")):
        return None
    return normalize_metric_key(match.group("name")[:SECTION_NAME_MAX])


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
    """updated_record_count reads Updated Record Count, and a sectioned key
    roads.updated_record_count reads Roads / Updated Record Count."""
    return " / ".join(" ".join(part.capitalize() for part in piece.split("_") if part)
                      for piece in str(metric_key).split("."))


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


def _tail_of(handle, codec, start, width, want, block=TAIL_BLOCK_BYTES):
    """The last `want` non-blank lines, read backwards from the end in blocks.

    Every block boundary sits a whole number of code units after the BOM, so a
    UTF-16 or UTF-32 file is never decoded from the middle of a character.
    """
    prev = handle.seek(0, 2)
    data = b""
    while True:
        pos = max(start, prev - block)
        pos -= (pos - start) % width
        handle.seek(pos)
        data = handle.read(prev - pos) + data
        prev = pos
        lines = tail_lines(data.decode(codec, "replace"), want, pos == start)
        if pos == start or len(lines) >= want:
            return lines


def scan_log_file(path, max_lines=DEFAULT_MAX_LINES, rules=(), tail=False, sections=False,
                  block=TAIL_BLOCK_BYTES):
    """The last value seen per metric in this file, plus line level counters.

    Last value wins within a file: a job that reports a running total reports
    the final one last. With tail, max_lines counts back from the end of the
    file instead of forward from its start. With sections, a key mined below a
    banner line is prefixed with the banner's name. The start time is always
    read from the head of the file, even with tail, because the tail of a long
    run is hours after it started.
    """
    metrics = {}
    lines_scanned = error_lines = warning_lines = 0

    try:
        handle = path.open("rb")
    except OSError as exc:
        _emit("WARNING: could not read %s: %s" % (path, exc))
        return {"metrics": {}, "lines_scanned": 0, "error_lines": 0, "warning_lines": 0,
                "started": None, "encoding": None}

    with handle:
        sample = handle.read(SNIFF_BYTES)
        codec, bom, width = sniff_encoding(sample)
        started = start_minutes(split_lines(sample[bom:].decode(codec, "replace")))
        if tail and max_lines > 0:
            lines = _tail_of(handle, codec, bom, width, max_lines, block)
        else:
            handle.seek(bom)
            lines = io.TextIOWrapper(handle, encoding=codec, errors="replace")

        section = None
        for raw_line in lines:
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

            # A banner is a title, not a measurement: it opens a section and is
            # not mined itself.
            banner = section_name(line) if sections else None
            if banner is not None:
                section = banner
            else:
                for key, value in mine_line(line, rules):
                    if section is not None:
                        key = section + "." + key
                    metrics[key] = (value, line[:240])

            if 0 < max_lines <= lines_scanned:
                break

    return {"metrics": metrics, "lines_scanned": lines_scanned,
            "error_lines": error_lines, "warning_lines": warning_lines,
            "started": started, "encoding": codec}


def build_rows(paths, rules=(), max_lines=DEFAULT_MAX_LINES, tail=False, sections=False,
               scheduled=(), slack=DEFAULT_SLACK_MINUTES):
    """(metric rows, per file summaries) for these log files.

    One row per (file, metric). The file's modification time is the
    observation timestamp, because a scheduled job's log file is one run.

    With scheduled times, a run that started more than slack minutes from
    every one of them is a manual run: it is read and counted, and its rows are
    left out of the series. A run with no timestamp at its start is kept with a
    warning, because it cannot be told either way.
    """
    rows = []
    summaries = []
    for path in paths:
        mtime = _mtime(path)
        if mtime is None:
            _emit("WARNING: %s vanished during the scan; skipping it" % path)
            continue
        observed = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        scan = scan_log_file(path, max_lines, rules, tail, sections)
        manual = False
        if scheduled and scan["started"] is None:
            _emit("WARNING: %s has no timestamp at its start, so a manual run cannot be "
                  "told from a scheduled one; kept" % path.name)
        elif scheduled:
            off = minutes_off_schedule(scan["started"], scheduled)
            if off > slack:
                manual = True
                _emit("left out %s as a manual run: it started at %s, %d minute(s) from "
                      "the nearest scheduled time" % (path.name, clock(scan["started"]), off))
        for key in ([] if manual else sorted(scan["metrics"])):
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
            "manual_run": manual,
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
    transcript_header = ("PSVersion: 5.1.26100.9444", "BuildVersion: 10.0.26100.9444",
                         "CLRVersion: 4.0.30319.42000", "WSManStackVersion: 3.0",
                         "PSRemotingProtocolVersion: 2.3", "SerializationVersion: 1.1.0.1",
                         "PSCompatibleVersions: 1.0, 2.0, 3.0, 4.0, 5.0, 5.1.26100.9444",
                         "Process ID: 27252", "End time: 20261009073656")
    check([line for line in transcript_header if mine_line(line)] == [],
          "no line of a start-transcript header is a metric  <-- pinned defect")
    check(mine_line("Reconciled versions: 12") == [("reconciled_versions", 12.0)],
          "a plural count of versions is still a metric")

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

    # ---- encoding sniffing
    text = "2026-08-11 03:14:07 - INFO - Records updated: 5\r\n"
    check(sniff_encoding(text.encode("utf-8")) == ("utf-8", 0, 1),
          "plain ascii is utf-8, as before")
    check(sniff_encoding(codecs.BOM_UTF8 + text.encode("utf-8")) == ("utf-8", 3, 1),
          "a utf-8 bom is found and skipped  <-- pinned defect")
    check(sniff_encoding(codecs.BOM_UTF16_LE + text.encode("utf-16-le")) == ("utf-16-le", 2, 2),
          "a utf-16le bom, as powershell 5.1 > and >> write, is found  <-- pinned defect")
    check(sniff_encoding(codecs.BOM_UTF16_BE + text.encode("utf-16-be")) == ("utf-16-be", 2, 2),
          "a utf-16be bom is found")
    check(sniff_encoding(codecs.BOM_UTF32_LE + text.encode("utf-32-le")) == ("utf-32-le", 4, 4),
          "a utf-32le bom is not mistaken for the utf-16le bom it starts with  <-- pinned defect")
    check(sniff_encoding(codecs.BOM_UTF32_BE + text.encode("utf-32-be")) == ("utf-32-be", 4, 4),
          "a utf-32be bom is found")
    check(sniff_encoding(text.encode("utf-16-le")) == ("utf-16-le", 0, 2),
          "utf-16le with no bom is told by its zero bytes")
    check(sniff_encoding(text.encode("utf-16-be")) == ("utf-16-be", 0, 2),
          "and utf-16be by which half of each pair holds them")
    check(sniff_encoding(b"") == ("utf-8", 0, 1), "an empty file is utf-8")
    check(sniff_encoding(b"\x00" * 64) == ("utf-8", 0, 1),
          "a file padded with nuls is not utf-16: zeros in both halves")
    check(sniff_encoding(b"Records updated: 5\n\xff\xfe not utf-8\n") == ("utf-8", 0, 1),
          "ff fe in the middle of a file is not a bom")
    check(sniff_encoding(b"x") == ("utf-8", 0, 1), "a one byte file is utf-8")

    # ---- lines from the end
    check(split_lines("a\r\nb\rc\nd") == ["a", "b", "c", "d"],
          "crlf, cr and lf each end a line, as universal newlines read them")
    check(tail_lines("a\n\nb\nc\n", 2, True) == ["b", "c"],
          "the tail is the last n non-blank lines")
    check(tail_lines("rds updated: 5\nb\nc", 5, False) == ["b", "c"],
          "a window that starts mid file drops its first line as a fragment  <-- pinned defect")
    check(tail_lines("a\nb", 5, True) == ["a", "b"],
          "a window from the start of the file keeps its first line")
    check(tail_lines("a\nb\nc", 0, True) == ["a", "b", "c"], "a tail of 0 is every line")

    # ---- the start of a run
    check(start_minutes(["2026-08-11 03:14:07,001 - INFO - start"]) == 194,
          "the first timestamp is the start, 03:14 is minute 194")
    check(start_minutes(["=====", "[2026-08-11 22:05:00] begin", "2026-08-11 23:59:00 x"]) == 1325,
          "a banner before it is skipped, and only the first stamp counts")
    check(start_minutes(["2026-08-11 25:14:07 bad", "2026-08-11 03:14:07 ok"]) is None,
          "an impossible first stamp is unknown, not a later line's time")
    check(start_minutes(["no stamp here", ""]) is None, "a log with no stamp has no start")
    check(parse_clock("03:14") == 194 and parse_clock(" 3:14 ") == 194,
          "a scheduled time is read as hh:mm")
    raises(lambda: parse_clock("24:00"), "an hour past 23 is refused")
    raises(lambda: parse_clock("12:60"), "a minute past 59 is refused")
    raises(lambda: parse_clock("noon"), "a word is not a time")
    check(clock(194) == "03:14", "minute 194 prints as 03:14")
    check(minutes_off_schedule(parse_clock("00:05"), [parse_clock("23:50")]) == 15,
          "a run 15 minutes past midnight is 15 minutes late, not 1425 early  <-- pinned defect")
    check(minutes_off_schedule(parse_clock("14:10"), [parse_clock("03:00"), parse_clock("14:00")]) == 10,
          "the nearest of several scheduled times is the one that counts")

    # ---- section banners
    check(section_name("=== Roads ===") == "roads", "a banner names its section")
    check(section_name("### Parcels (pass 2) ###") == "parcels_pass_2",
          "a section name becomes a key like any label")
    check(section_name("2026-08-11 03:14:07 - INFO - ----- Step 2: Roads -----") == "step_2_roads",
          "a banner behind a logging preamble is still a banner")
    check(section_name("2026-08-11 03:14:07 --- Roads ---") == "roads",
          "a dash fence behind a timestamp is not eaten as a separator  <-- pinned defect")
    check(section_name("==========") is None, "a rule line with no name is not a banner")
    check(section_name("=== 5 ===") is None, "a banner with no letter in it is not a section")
    check(section_name("--- retrying") is None, "a one sided fence is a message, not a banner")
    check(section_name("=== Roads ---") is None, "mismatched fences are not a banner")
    check(section_name("Records updated: 5") is None, "an ordinary line is not a banner")
    check(len(section_name("=== %s ===" % ("x" * 90))) == 40, "a long section name is cut at 40")
    check(title_case_metric("roads.updated_record_count") == "Roads / Updated Record Count",
          "a sectioned key gets a readable display name")

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
        # One assertion either way, written as expressions so that both hosts
        # run the same line and report the same count.
        folds = os.path.normcase("A") == "a"
        check(len(folded) == (1 if folds else 2),
              "Logs and LOGS are ONE directory where the filesystem folds case  <-- pinned defect"
              if folds else
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

        # ---- the pinned defect: every encoding a windows job writes
        def wbytes(name, data):
            path = os.path.join(tmp, "enc", name)
            parent = os.path.dirname(path)
            if not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "wb") as handle:
                handle.write(data)
            return Path(path)

        run = ("2026-08-11 03:14:07 - INFO - Records updated: 1,432\r\n"
               "2026-08-11 03:14:09 - INFO - Duration: 0:00:07\r\n")
        want = {"updated_record_count": 1432.0, "duration_seconds": 7.0}
        bom8 = scan_log_file(wbytes("bom8.log", codecs.BOM_UTF8 + run.encode("utf-8")))
        check(dict((key, pair[0]) for key, pair in bom8["metrics"].items()) == want,
              "a utf-8 bom log, as start-transcript writes, mines the same two metrics  <-- pinned defect")
        check(not [key for key in bom8["metrics"] if key.startswith("info_")],
              "and the bom does not bring back the info_ key on line one  <-- pinned defect")
        for name, data in (("ps51.log", codecs.BOM_UTF16_LE + run.encode("utf-16-le")),
                           ("be16.log", codecs.BOM_UTF16_BE + run.encode("utf-16-be")),
                           ("le32.log", codecs.BOM_UTF32_LE + run.encode("utf-32-le")),
                           ("be32.log", codecs.BOM_UTF32_BE + run.encode("utf-32-be")),
                           ("nobom16.log", run.encode("utf-16-le"))):
            got = scan_log_file(wbytes(name, data))
            check(dict((key, pair[0]) for key, pair in got["metrics"].items()) == want
                  and got["lines_scanned"] == 2,
                  "%s (%s) mines the same two metrics from two lines  <-- pinned defect"
                  % (name, got["encoding"]))
        # Limits the README names. A BOM-less UTF-32 file, and a file whose
        # encoding changes part way, as ">>" in PowerShell 5.1 makes when it
        # appends UTF-16LE to a UTF-8 log, are decoded by their start.
        check(scan_log_file(wbytes("nobom32.log", run.encode("utf-32-le")))["metrics"] == {},
              "a utf-32 log with no bom mines nothing: a named limit")
        mixed = scan_log_file(wbytes("mixed.log", b"Records updated: 3\r\n" * 10
                                     + codecs.BOM_UTF16_LE + "Records checked: 9\r\n".encode("utf-16-le")))
        check(list(mixed["metrics"]) == ["updated_record_count"],
              "a utf-16 tail appended to a utf-8 log is not mined: a named limit")

        # ---- tail read
        check(scan_log_file(Path(running), max_lines=2, tail=True)["metrics"]["updated_record_count"][0]
              == 30.0,
              "--tail reads the last lines, so the final total is the one mined  <-- pinned defect")
        check(scan_log_file(Path(running), max_lines=2, tail=True)["lines_scanned"] == 2,
              "and still reads only --max-lines lines")
        check(scan_log_file(Path(running), max_lines=0, tail=True)["lines_scanned"] == 3,
              "--tail with --max-lines 0 reads the whole file")
        check(scan_log_file(Path(running), max_lines=99, tail=True)["lines_scanned"] == 3,
              "a tail longer than the file is the whole file")
        long_text = "".join("2026-08-11 03:%02d:00 - INFO - Records updated: %d\n" % (i % 60, i)
                            for i in range(1, 401))
        longlog = wbytes("long.log", long_text.encode("utf-8"))
        small = scan_log_file(longlog, max_lines=3, tail=True, block=64)
        check(small["metrics"]["updated_record_count"][0] == 400.0 and small["lines_scanned"] == 3,
              "a tail read in small blocks stops after a few blocks with the last value")
        check(small["started"] == 181, "and the start time still comes from the head of the file")
        long16 = wbytes("long16.log", codecs.BOM_UTF16_LE + long_text.encode("utf-16-le"))
        for block in (61, 64, 4099):
            got = scan_log_file(long16, max_lines=7, tail=True, block=block)
            check(got["metrics"]["updated_record_count"][0] == 400.0 and got["lines_scanned"] == 7,
                  "a utf-16 tail read in %d byte blocks stays on character boundaries  "
                  "<-- pinned defect" % block)
        # A misaligned block decodes as noise with no newline in it, so the read
        # would quietly run back to the start of the file and still give the
        # right answer. Only the lowest byte it sought to shows the difference.
        class SeekSpy(io.BytesIO):
            lowest = None

            def seek(self, *args):
                where = io.BytesIO.seek(self, *args)
                self.lowest = where if self.lowest is None else min(self.lowest, where)
                return where

        data16 = codecs.BOM_UTF16_LE + long_text.encode("utf-16-le")
        spy = SeekSpy(data16)
        got = _tail_of(spy, "utf-16-le", 2, 2, 7, 61)
        check(got[-1].endswith("Records updated: 400") and len(got) == 7
              and spy.lowest > len(data16) - 61 * 12,
              "a utf-16 tail stops after a few blocks, not at the start of the file  <-- pinned defect")
        accents = "".join("Records updated: %d \xe9t\xe9\n" % i for i in range(1, 50))
        accented_log = wbytes("accents.log", accents.encode("utf-8"))
        cut = [scan_log_file(accented_log, max_lines=5, tail=True, block=block)
               for block in range(20, 40)]
        check(all(got["metrics"]["updated_record_count"] == (49.0, "Records updated: 49 \xe9t\xe9")
                  and got["lines_scanned"] == 5 for got in cut),
              "a block edge inside a two byte character never reaches a kept line")
        check(scan_log_file(Path(noisy), max_lines=1, tail=True)["error_lines"] == 0,
              "error lines are counted only inside the tail that was read")

        # ---- sections
        sectioned = write(os.path.join(tmp, "sections.log"),
                          "2026-08-11 03:14:00 - INFO - Records checked: 70\n"
                          "2026-08-11 03:14:01 - INFO - === Parcels ===\n"
                          "2026-08-11 03:14:02 - INFO - Records updated: 10\n"
                          "2026-08-11 03:14:03 - INFO - ===== Road Centerlines =====\n"
                          "2026-08-11 03:14:04 - INFO - Records updated: 5\n")
        flat = scan_log_file(Path(sectioned))
        check(flat["metrics"]["updated_record_count"][0] == 5.0
              and "parcels.updated_record_count" not in flat["metrics"],
              "without --sections the second section overwrites the first, as before")
        split = scan_log_file(Path(sectioned), sections=True)
        check(split["metrics"]["parcels.updated_record_count"][0] == 10.0
              and split["metrics"]["road_centerlines.updated_record_count"][0] == 5.0,
              "with --sections each section is its own series  <-- pinned defect")
        check(split["metrics"]["checked_record_count"][0] == 70.0,
              "a metric above the first banner keeps its plain key")
        check(len(split["metrics"]) == 3 and split["lines_scanned"] == 5,
              "a banner line is counted but never mined")
        titled = write(os.path.join(tmp, "titled.log"), "=== Updated 40 records ===\nRecords updated: 5\n")
        check(scan_log_file(Path(titled), sections=True)["metrics"]
              == {"updated_40_records.updated_record_count": (5.0, "Records updated: 5")},
              "a number inside a banner names the section and is not mined as a metric")
        trailing = write(os.path.join(tmp, "trailing.log"),
                         "=== Roads ===\nRecords updated: 5\nDuration: 0:00:09\n")
        check(list(scan_log_file(Path(trailing), sections=True)["metrics"])
              == ["roads.updated_record_count", "roads.duration_seconds"],
              "a job total after the last banner is keyed to that section: a named limit")
        rows, _ = build_rows([Path(sectioned)], sections=True)
        check([row["display_name"] for row in rows][1] == "Parcels / Updated Record Count",
              "a sectioned row carries a readable display name")

        # ---- manual runs
        manual_log = write(os.path.join(tmp, "manual", "run_1042.log"),
                           "2026-08-11 10:42:00 - INFO - Records updated: 3\n")
        late_log = write(os.path.join(tmp, "manual", "run_0320.log"),
                         "2026-08-11 03:20:00 - INFO - Records updated: 1400\n")
        bare_log = write(os.path.join(tmp, "manual", "run_bare.log"), "Records updated: 9\n")
        at = [parse_clock("03:14")]
        (rows, summaries), _, warned = capture(
            lambda: build_rows([Path(manual_log), Path(late_log)], scheduled=at))
        check([row["metric_value"] for row in rows] == [1400.0],
              "a run at 10:42 for a job scheduled at 03:14 is left out of the series  <-- pinned defect")
        check([summary["manual_run"] for summary in summaries] == [True, False],
              "and is still counted as a file that was read")
        check("left out run_1042.log" in warned and "10:42" in warned and "448 minute" in warned,
              "and stderr says which run, when it started and how far off it was")
        rows, _ = build_rows([Path(manual_log), Path(late_log)])
        check(len(rows) == 2, "without --scheduled-at no run is left out, as before")
        rows, _ = build_rows([Path(manual_log)], scheduled=at, slack=500)
        check(len(rows) == 1, "a slack wide enough to reach the run keeps it")
        (rows, _), _, warned = capture(lambda: build_rows([Path(bare_log)], scheduled=at))
        check(len(rows) == 1 and "no timestamp at its start" in warned,
              "a run with no timestamp is kept, with a warning that it could not be told")
        slow = write(os.path.join(tmp, "slow", "run.log"),
                     "2026-08-11 03:14:00 - INFO - start\n"
                     + "2026-08-11 04:00:00 - INFO - working\n" * 40
                     + "2026-08-11 05:30:00 - INFO - Records updated: 77\n")
        rows, _ = build_rows([Path(slow)], max_lines=2, tail=True, scheduled=at)
        check([row["metric_value"] for row in rows] == [77.0],
              "a long run read with --tail is judged by its first line, not its last  <-- pinned defect")

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
        args = _parse([])
        check(args.tail is False and args.sections is False and args.scheduled_at is None
              and args.slack == 30,
              "--tail, --sections and --scheduled-at are off by default, and slack is 30 minutes")
        args = _parse(["x", "--tail", "--sections", "--scheduled-at", "03:14",
                       "--scheduled-at", "23:50", "--slack", "5"])
        check(args.tail is True and args.sections is True and args.scheduled_at == [194, 1430]
              and args.slack == 5,
              "every new flag is read, and --scheduled-at repeats")
        for bad_argv, label in ((["--scheduled-at", "25:00"], "an impossible --scheduled-at"),
                                (["--sched", "03:14"], "a prefix of --scheduled-at"),
                                (["--tai"], "a prefix of --tail")):
            try:
                code = None
                _, _, err = capture(lambda: _parse(bad_argv))
            except SystemExit as exc:
                code = exc.code
            check(code == 2, "%s is a usage error" % label)

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

        # ---- the new modes, end to end
        rc, out, err = capture(lambda: main([str(logdir), "--tail", "--sections"]))
        check(rc == 64 and "cannot be combined" in err and out == "",
              "--tail with --sections is a usage error, not a silently wrong key")
        rc, out, err = capture(lambda: main([str(logdir), "--slack", "-1"]))
        check(rc == 64 and "--slack" in err, "a negative --slack is a usage error")
        rc, out, err = capture(lambda: main([os.path.join(tmp, "enc", "ps51.log"), "--format", "csv"]))
        check(rc == 0 and "updated_record_count,Updated Record Count,1432," in out,
              "a powershell 5.1 utf-16 log mines through main  <-- pinned defect")
        rc, out, err = capture(lambda: main([str(longlog), "--tail", "--max-lines", "1"]))
        check(rc == 0 and " 400 " in out, "--tail through main mines the final value")
        rc, out, err = capture(lambda: main([sectioned, "--sections"]))
        check(rc == 0 and "parcels.updated_record_count" in out
              and "road_centerlines.updated_record_count" in out,
              "--sections through main charts two series")
        rc, out, err = capture(lambda: main([os.path.dirname(manual_log), "--days", "36500",
                                             "--scheduled-at", "03:14"]))
        check(rc == 0 and "1400" in out and " 3 " not in out
              and "1 manual run(s) left out" in err,
              "--scheduled-at through main leaves the manual run out and counts it")
        rc, out, err = capture(lambda: main([manual_log, "--scheduled-at", "03:14"]))
        check(rc == 1 and "no number" in err,
              "a series of only manual runs is exit 1, not an empty success")
        rc, out, err = capture(lambda: main([str(logdir)]))
        check("manual run" not in err, "and without --scheduled-at stderr is as before")

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
        # ponytail: the next four lines run only when check() itself is broken,
        # so a green run cannot reach them. Coverage reports them as missed.
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
    check(footer(5, ["x"]) == ["5 assertions, 1 failed", "  FAILED: x"],
          "a failed run's footer names the count and each failure")

    print("-" * 68)
    for line in footer(passed[0] + len(failed), failed):
        print(line)
    return 1 if failed else 0


def footer(total, failed):
    """The self-test's last lines. A function so the failure branch is tested."""
    if failed:
        return ["%d assertions, %d failed" % (total, len(failed))] + [
            "  FAILED: %s" % label for label in failed]
    return ["%d assertions, 0 failed" % total]


# ----------------------------------------------------------------------- cli

def _clock_arg(text):
    try:
        return parse_clock(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


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
    parser.add_argument("--tail", action="store_true",
                        help="take --max-lines from the END of each file, where a job "
                             "writes its totals, instead of from the start")
    parser.add_argument("--sections", action="store_true",
                        help="prefix each key with the banner section it sits under, "
                             "such as === Roads ===, so two sections are two series")
    parser.add_argument("--scheduled-at", dest="scheduled_at", action="append",
                        type=_clock_arg, metavar="HH:MM",
                        help="a time the job is scheduled to start, in the clock its "
                             "log is written in. Repeat it for several. A run that "
                             "started further than --slack from all of them is a "
                             "manual run and is left out of the series.")
    parser.add_argument("--slack", type=int, default=DEFAULT_SLACK_MINUTES, metavar="N",
                        help="minutes a scheduled run may start late or early "
                             "(default 30)")
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

    if args.slack < 0:
        _emit("error: --slack is a number of minutes, 0 or more")
        return 64
    # A tail window starts part way through a section, so the lines above its
    # first banner would be keyed to no section and join the wrong series.
    if args.tail and args.sections:
        _emit("error: --tail and --sections cannot be combined: the lines above the "
              "first banner in the tail would be keyed to no section")
        return 64

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

    rows, summaries = build_rows(files, rules, args.max_lines, args.tail, args.sections,
                                 args.scheduled_at or (), args.slack)
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
    if args.scheduled_at:
        _emit("logsift: %d manual run(s) left out of the series"
              % sum(1 for summary in summaries if summary["manual_run"]))
    if not rows:
        _emit("no number in those logs survived the rules. Run with --rules to "
              "name the labels this job uses.")
        return 1
    return 0


# ponytail: run as a script this test is always true, so coverage reports its
# false branch, the import case, as missed.
if __name__ == "__main__":
    sys.exit(main())
