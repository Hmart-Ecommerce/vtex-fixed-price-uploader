import json
import os
import stat

import pytest

from vtex_fixed_price_uploader.writelog import (
    InputsChanged, NoOpenRun, UnfinishedRun, WriteLog, sha256_of)


def test_sha256_is_stable_across_key_order():
    assert sha256_of({"a": 1, "b": 2}) == sha256_of({"b": 2, "a": 1})


def test_sha256_differs_on_content():
    assert sha256_of({"a": 1}) != sha256_of({"a": 2})


def test_begin_writes_a_start_record(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    run_id = log.begin(10, "csv1", "snap1")
    lines = (tmp_path / "w.jsonl").read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["kind"] == "start"
    assert record["expected_rows"] == 10
    assert record["run_id"] == run_id


def test_append_persists_immediately(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(2, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    # read from a different handle: the row must already be on disk
    lines = path.read_text().strip().splitlines()
    assert json.loads(lines[1])["sku"] == "111"


def test_done_pairs_only_counts_successes(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(3, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    log.append("222", "acct_one", 500, 0)
    log.append("333", "acct_one", 0, 0)
    assert log.done_pairs() == {("111", "acct_one")}


def test_finish_closes_the_run(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(1, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    log.finish()
    assert log.unfinished() is None


def test_unfinished_run_is_detected(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    pending = WriteLog(path).unfinished()
    assert pending["expected_rows"] == 5


def test_begin_refuses_while_a_run_is_open(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    with pytest.raises(UnfinishedRun):
        WriteLog(path).begin(5, "csv1", "snap1")


def test_resume_returns_the_open_run_id(tmp_path):
    path = str(tmp_path / "w.jsonl")
    first = WriteLog(path).begin(5, "csv1", "snap1")
    assert WriteLog(path).resume("csv1", "snap1") == first


def test_resume_refuses_a_different_csv(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    with pytest.raises(InputsChanged) as exc:
        WriteLog(path).resume("csv2", "snap1")
    assert "file" in str(exc.value).lower()


def test_resume_refuses_a_different_snapshot(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    with pytest.raises(InputsChanged):
        WriteLog(path).resume("csv1", "snap2")


def test_discard_clears_the_open_run(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    log = WriteLog(path)
    log.discard()
    assert log.unfinished() is None
    assert log.begin(5, "csv1", "snap1")


def test_resume_after_discard_starts_clean(tmp_path):
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(2, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    log.discard()
    log.begin(2, "csv1", "snap1")
    assert log.done_pairs() == set()


def test_a_corrupt_line_does_not_crash_reading(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(1, "csv1", "snap1")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    log.append("111", "acct_one", 200, 1)
    assert log.done_pairs() == {("111", "acct_one")}


def _count_fsyncs(monkeypatch):
    """Record every fd handed to os.fsync, split into files and directories."""
    real_fsync = os.fsync
    files, directories = [], []
    def recording_fsync(fd):
        bucket = directories if stat.S_ISDIR(os.fstat(fd).st_mode) else files
        bucket.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    return files, directories


def test_every_appended_record_is_fsynced(tmp_path, monkeypatch):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(2, "csv1", "snap1")
    files, _ = _count_fsyncs(monkeypatch)
    log.append("111", "acct_one", 200, 1)
    assert len(files) == 1
    log.append("222", "acct_one", 200, 1)
    assert len(files) == 2
    log.finish()
    assert len(files) == 3


def test_the_containing_directory_is_fsynced_once_on_creation(tmp_path, monkeypatch):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    _, directories = _count_fsyncs(monkeypatch)
    log.begin(1, "csv1", "snap1")
    assert len(directories) == 1
    log.append("111", "acct_one", 200, 1)
    assert len(directories) == 1


def test_a_torn_tail_does_not_swallow_the_next_row(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(3, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    # a process killed mid-line: a fragment with no trailing newline
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "row", "at": "2026-01-01T00:00:00+00:00", '
                 '"sku": "333", "acc')
    resumed = WriteLog(str(path))
    resumed.resume("csv1", "snap1")
    resumed.append("333", "acct_one", 200, 1)
    assert ("333", "acct_one") in resumed.done_pairs()


def test_a_corrupt_line_without_a_trailing_newline_is_skipped(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(1, "csv1", "snap1")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    log.append("111", "acct_one", 200, 1)
    assert log.done_pairs() == {("111", "acct_one")}


def test_a_line_that_is_not_an_object_is_skipped(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(1, "csv1", "snap1")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("123\n")
    log.append("111", "acct_one", 200, 1)
    assert log.done_pairs() == {("111", "acct_one")}


def test_a_row_missing_a_key_does_not_abort_the_resume(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path))
    log.begin(2, "csv1", "snap1")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "row", "at": "2026-01-01T00:00:00+00:00",
                             "account": "acct_one", "status": 200,
                             "entries": 1}) + "\n")
    log.append("111", "acct_one", 200, 1)
    assert log.done_pairs() == {("111", "acct_one")}


def test_append_coerces_sku_and_account_to_str(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(1, "csv1", "snap1")
    log.append(111, "acct_one", 200, 1)
    assert log.done_pairs() == {("111", "acct_one")}


def test_the_timestamp_source_is_injectable(tmp_path):
    path = tmp_path / "w.jsonl"
    log = WriteLog(str(path), now=lambda: "2026-01-01T00:00:00+00:00")
    log.begin(1, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    records = [json.loads(line)
               for line in path.read_text().strip().splitlines()]
    assert [record["at"] for record in records] == [
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]


def test_the_public_interface_is_annotated():
    assert WriteLog.begin.__annotations__["return"] is str
    assert WriteLog.resume.__annotations__["return"] is str
    assert WriteLog.unfinished.__annotations__["return"] == (dict | None)
    assert WriteLog.done_pairs.__annotations__["return"] == set[tuple[str, str]]
    assert sha256_of.__annotations__["return"] is str


def test_nothing_to_resume_is_a_different_type_from_a_run_still_open():
    assert NoOpenRun is not UnfinishedRun
    assert not issubclass(NoOpenRun, UnfinishedRun)
    assert not issubclass(UnfinishedRun, NoOpenRun)


def test_resume_without_a_log_file_says_there_is_nothing_to_resume(tmp_path):
    with pytest.raises(NoOpenRun):
        WriteLog(str(tmp_path / "missing.jsonl")).resume("csv1", "snap1")


def test_resume_on_an_empty_log_says_there_is_nothing_to_resume(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text("")
    with pytest.raises(NoOpenRun):
        WriteLog(str(path)).resume("csv1", "snap1")


def test_resume_on_a_finished_run_says_there_is_nothing_to_resume(tmp_path):
    path = str(tmp_path / "w.jsonl")
    log = WriteLog(path)
    log.begin(1, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    log.finish()
    with pytest.raises(NoOpenRun):
        WriteLog(path).resume("csv1", "snap1")


def test_resume_with_rows_but_no_header_is_reported_as_corruption(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text(json.dumps({"kind": "row", "at": "2026-01-01T00:00:00+00:00",
                                "sku": "111", "account": "acct_one",
                                "status": 200, "entries": 1}) + "\n",
                    encoding="utf-8")
    with pytest.raises(InputsChanged):
        WriteLog(str(path)).resume("csv1", "snap1")


def test_resume_with_an_unreadable_header_is_reported_as_corruption(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text('{"kind": "start", "run_id": "abc", "expec',
                    encoding="utf-8")
    with pytest.raises(InputsChanged):
        WriteLog(str(path)).resume("csv1", "snap1")


def test_begin_still_raises_unfinished_run_while_a_run_is_open(tmp_path):
    path = str(tmp_path / "w.jsonl")
    WriteLog(path).begin(5, "csv1", "snap1")
    with pytest.raises(UnfinishedRun):
        WriteLog(path).begin(5, "csv1", "snap1")


def test_append_without_a_begin_is_refused(tmp_path):
    with pytest.raises(NoOpenRun):
        WriteLog(str(tmp_path / "w.jsonl")).append("111", "acct_one", 200, 1)


def test_append_after_finish_is_refused(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(1, "csv1", "snap1")
    log.append("111", "acct_one", 200, 1)
    log.finish()
    with pytest.raises(NoOpenRun):
        log.append("222", "acct_one", 200, 1)


def test_append_after_discard_is_refused(tmp_path):
    log = WriteLog(str(tmp_path / "w.jsonl"))
    log.begin(1, "csv1", "snap1")
    log.discard()
    with pytest.raises(NoOpenRun):
        log.append("222", "acct_one", 200, 1)
