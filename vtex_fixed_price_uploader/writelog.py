"""The record of what was actually written, on disk, one row at a time.

Every row is appended and fsynced as it completes. Buffering in memory and
writing at the end is the same as having no log: the moment the process dies -
a closed tab, a dropped connection - the record of writes already made dies
with it, and nobody can tell what production now holds.

Format is JSON Lines: one `start` record, one `row` per write, one `end`. A run
without an `end` is unfinished, and a later run refuses to begin until someone
decides to resume or discard it.

A newline is a terminator, not a leader, so a process killed mid-line leaves a
fragment with no trailing newline. Every append therefore closes any such
fragment with its own leading newline first: without it the fragment and the
next record fuse into one unparseable line and the next row - already written
to production - vanishes from the log.
"""

import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timezone


class UnfinishedRun(Exception):
    """A previous run is still open; resume or discard it before beginning."""


class NoOpenRun(Exception):
    """There is no open run: nothing to resume, and nothing to append to."""


class InputsChanged(Exception):
    """The open run used different inputs, or its header is unreadable."""


def sha256_of(obj) -> str:
    """A stable digest of any JSON-serialisable value."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WriteLog:
    def __init__(self, path: str, now: Callable[[], str] = _now) -> None:
        self.path = path
        self._now = now
        self._run_open = False

    def _records(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        # errors="replace" so a torn multi-byte tail degrades to an
        # unparseable line rather than an exception.
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue      # a torn write; skip it rather than crash
                if isinstance(record, dict):
                    out.append(record)
        return out

    def _has_content(self) -> bool:
        if not os.path.exists(self.path):
            return False
        with open(self.path, encoding="utf-8", errors="replace") as fh:
            return any(line.strip() for line in fh)

    def _ends_with_newline(self) -> bool:
        """False only when a torn write left a fragment at the tail."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return True
        if size == 0:
            return True
        with open(self.path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"

    def _fsync_directory(self) -> None:
        """Persist the directory entry itself, so a crash cannot lose it."""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_record(self, record: dict) -> None:
        is_new = not os.path.exists(self.path)
        lead = "" if is_new or self._ends_with_newline() else "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(lead + json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if is_new:
            self._fsync_directory()

    def _open_run(self, records: list[dict]) -> dict | None:
        open_run = None
        for record in records:
            if record.get("kind") == "start":
                open_run = record
            elif record.get("kind") == "end":
                open_run = None
        return open_run

    def unfinished(self) -> dict | None:
        """The start record of an open run, or None."""
        return self._open_run(self._records())

    def begin(self, expected_rows: int, csv_hash: str,
              snapshot_hash: str) -> str:
        if self.unfinished() is not None:
            raise UnfinishedRun(
                "A previous upload did not finish. Resume it or discard it "
                "before starting a new one.")
        run_id = uuid.uuid4().hex
        self._append_record({
            "kind": "start", "run_id": run_id, "at": self._now(),
            "expected_rows": expected_rows,
            "csv_sha256": csv_hash, "snapshot_sha256": snapshot_hash,
        })
        self._run_open = True
        return run_id

    def resume(self, csv_hash: str, snapshot_hash: str) -> str:
        records = self._records()
        pending = self._open_run(records)
        if pending is None:
            if self._has_content() and not any(
                    record.get("kind") == "start" for record in records):
                raise InputsChanged(
                    "The upload log has content but no readable run header, "
                    "so it is damaged. Discard it and start again.")
            raise NoOpenRun("There is no unfinished upload to resume.")
        if pending.get("csv_sha256") != csv_hash:
            raise InputsChanged(
                "The unfinished upload used a different file. Discard it and "
                "start again, or upload the original file.")
        if pending.get("snapshot_sha256") != snapshot_hash:
            raise InputsChanged(
                "Prices in VTEX changed since the unfinished upload started. "
                "Discard it and start again.")
        self._run_open = True
        return pending["run_id"]

    def append(self, sku, account, status: int, entries: int) -> None:
        if not self._run_open:
            raise NoOpenRun(
                "There is no open upload to record this row against. Begin a "
                "new upload or resume the unfinished one first.")
        self._append_record({
            "kind": "row", "at": self._now(),
            "sku": str(sku), "account": str(account),
            "status": status, "entries": entries,
        })

    def finish(self) -> None:
        self._append_record({"kind": "end", "at": self._now()})
        self._run_open = False

    def done_pairs(self) -> set[tuple[str, str]]:
        """Pairs ATTEMPTED in the currently open run, still to be verified.

        A 200 means the write was accepted, not that production now holds the
        new price - only the read-back verification phase can say that. These
        pairs are skipped on resume because the write is a full-array
        replacement and so re-issuing it is idempotent, which makes re-writing
        thousands of rows on every resume pure cost. Verification covers every
        row in the log, not only the rows written in the current session.
        """
        pairs, collecting = set(), False
        for record in self._records():
            kind = record.get("kind")
            if kind == "start":
                pairs, collecting = set(), True
            elif kind == "end":
                collecting = False
            elif kind == "row" and collecting and record.get("status") == 200:
                sku, account = record.get("sku"), record.get("account")
                if sku is None or account is None:
                    continue      # an incomplete record; skip it, do not crash
                pairs.add((sku, account))
        return pairs

    def discard(self) -> None:
        """Close the open run without claiming it succeeded."""
        if self.unfinished() is not None:
            self._append_record({"kind": "end", "at": self._now(),
                                 "discarded": True})
        self._run_open = False
