"""Acknowledgement state and the optional notebook interface.

The readiness logic lives in ``AckState`` so the production-write gate can be
tested without notebook dependencies. The optional widgets are imported only
inside the functions that use them.
"""


class AckState:
    """Track acknowledgement of each warning group and typed confirmation."""

    def __init__(self, model):
        self.model = model
        self._required = set(model.ack_keys or ())
        self._ticked = set()
        self._typed = ""

    def tick(self, key, checked=True):
        if key not in self._required:
            return
        if checked:
            self._ticked.add(key)
        else:
            self._ticked.discard(key)

    def set_typed(self, text):
        self._typed = str(text or "")

    @property
    def _typed_ok(self):
        if not self.model.needs_typed_confirmation:
            return True
        cleaned = self._typed.replace(",", "").replace(" ", "").strip()
        return cleaned.isdigit() and int(cleaned) == self.model.write_rows

    @property
    def ready(self):
        if not self.model.write_rows:
            return False
        return not (self._required - self._ticked) and self._typed_ok

    @property
    def blocking_reason(self):
        if not self.model.write_rows:
            return "There is nothing to upload."
        outstanding = len(self._required - self._ticked)
        if outstanding:
            return "Tick the remaining {} item{} above.".format(
                outstanding, "" if outstanding == 1 else "s")
        if not self._typed_ok:
            return "Type {} to confirm.".format(self.model.write_rows)
        return ""


def verification_summary(check):
    """Return an operator-facing, exhaustive read-back accounting.

    ``still_multiple`` is an additional counter in ``VerifyResult`` and can
    overlap its matched or mismatched totals. For the visible accounting, each
    row is assigned exactly once, with several live prices taking precedence.
    """
    counts = {
        "matched": 0,
        "mismatched": 0,
        "still_multiple": 0,
        "confirmed_empty": 0,
        "unreadable": 0,
    }
    for row in check.rows:
        verdict = row.get("verdict")
        if verdict == "confirmed_empty":
            counts["confirmed_empty"] += 1
        elif verdict == "unreadable":
            counts["unreadable"] += 1
        elif row.get("live_entries", 0) > 1:
            counts["still_multiple"] += 1
        elif verdict == "match":
            counts["matched"] += 1
        elif verdict == "mismatch":
            counts["mismatched"] += 1
        else:
            counts["unreadable"] += 1

    attempted = len(check.rows)
    accounting = (
        "{attempted:,} attempted = {matched:,} matched + "
        "{mismatched:,} mismatched + {still_multiple:,} still multiple + "
        "{confirmed_empty:,} confirmed empty + {unreadable:,} unreadable."
    ).format(attempted=attempted, **counts)
    confirmed = (
        "Confirmed empty: {confirmed_empty:,} confirmed failure; the price "
        "did not land and must be written again."
    ).format(**counts)
    unreadable = (
        "Unreadable: {unreadable:,} open question; these could not be checked "
        "- re-run the verification."
    ).format(**counts)
    return " {} {}".format(accounting, confirmed + " " + unreadable).strip()


def _scrub(text, token):
    """Never let the credential travel into anything the operator can see."""
    text = str(text)
    if token and str(token) in text:
        text = text.replace(str(token), "[login hidden]")
    return text


def _as_sentence(text):
    text = " ".join(str(text).split())
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else text + "."


def _with_next_step(sentence, step):
    """Append a next step unless the message already carries one."""
    if any(word in sentence.lower() for word in ("again", "ask ", "send ")):
        return sentence
    return "{} {}".format(sentence, step)


def _error_sentences():
    """(exception type, operator sentence, pass_through) - specific first.

    Imported lazily so this module keeps its no-import-at-module-scope shape.
    """
    from vtex_fixed_price_uploader.auth import TokenExpiringSoon
    from vtex_fixed_price_uploader.config import DisallowedAccount
    from vtex_fixed_price_uploader.reader import (
        AuthenticationError, UnhealthySnapshot)
    from vtex_fixed_price_uploader.runner import CredentialRejected
    from vtex_fixed_price_uploader.writelog import (
        InputsChanged, NoOpenRun, UnfinishedRun)

    fresh_login = "Get a fresh login and run this again."
    abandon = ("Upload the original file again to finish that run, or tick "
               "\"Abandon the unfinished upload log\" and start over - rows "
               "already written stay written in VTEX.")
    return (
        (TokenExpiringSoon, None, fresh_login),
        (CredentialRejected, None, fresh_login),
        (AuthenticationError,
         "Your login was rejected while prices were being read, so nothing "
         "was uploaded. Get a fresh login and run the check again.", None),
        (UnhealthySnapshot,
         "Too many price reads failed, so this check cannot be trusted and "
         "nothing was uploaded. Wait a few minutes and run the check again.",
         None),
        (InputsChanged,
         "This file does not match the upload that was interrupted earlier. "
         + abandon, None),
        (UnfinishedRun,
         "An earlier upload was interrupted and its log is still open. "
         + abandon, None),
        (NoOpenRun,
         "The upload log is not open, so nothing more could be recorded or "
         "written. Run the check again to start a new upload.", None),
        (DisallowedAccount,
         "One of the accounts in your file is not allowed to receive prices, "
         "so nothing was uploaded. Remove those rows from the file, or ask "
         "for that account to be added to accounts.json.", None),
        (ValueError,
         "Your file could not be read. Correct that value in the CSV and run "
         "the check again.", None),
        (OSError,
         "A file could not be opened. Check the folder in Drive and that the "
         "file is still there, then run the check again.", None),
    )


GENERIC_FAILURE = ("Something went wrong and nothing further was done. Send "
                   "this message to whoever set this tool up, then run the "
                   "check again.")


def friendly_error(exc, token=""):
    """One sentence, a next step, and never a traceback.

    Spec section 10: every failure the operator can hit renders as a sentence
    they can act on. The technical detail is kept, last, because it is what
    makes a support message useful - but it is never the whole answer, and it
    never carries the credential.
    """
    detail = _as_sentence(_scrub(exc, token))
    for kind, sentence, step in _error_sentences():
        if isinstance(exc, kind):
            if sentence is None:
                return _with_next_step(detail, step)
            return "{} Technical detail: {}".format(sentence, detail)
    return "{} Technical detail: {}".format(GENERIC_FAILURE, detail)


def outcome_lines(result):
    """Account for every row of a write pass - written, failed and skipped.

    Failed and skipped are stated even when they are zero. A row that came
    back 500, 400, or the status-0 network sentinel is otherwise invisible,
    and "Wrote 349 prices" is then read as "349 of 349".
    """
    lines = []
    if result.halted:
        lines.append(_as_sentence(result.halted))
    lines.append("Wrote {:,} price{} in this pass.".format(
        result.written, "" if result.written == 1 else "s"))
    if result.failed:
        lines.append(
            "{:,} rows failed to write. Those prices did NOT land and must "
            "be uploaded again.".format(result.failed))
    else:
        lines.append("0 rows failed to write.")
    if result.skipped:
        lines.append(
            "{:,} rows were already written by an earlier run, so this pass "
            "skipped them.".format(result.skipped))
    else:
        lines.append("0 rows were skipped as already written.")
    return tuple(lines)


SETTLE_NOTICE = (
    "Now waiting about two minutes before reading the rows back. VTEX's "
    "pricing takes roughly that long to settle, and reading sooner reports "
    "failures that are not real. Nothing is wrong and nothing is stuck - "
    "please do not close this page or run this again while it waits.")


def apply_and_verify(config, pre, token, log, say, progress=None,
                     abandon_unfinished=False, apply_fn=None, verify_fn=None):
    """Write, then read back - on the halt path as well as the clean one.

    A halt usually arrives after hundreds of rows are already in VTEX. An
    HTTP 200 from the write endpoint proves nothing, so skipping the read-back
    on the halt path leaves the rows that were written as the only ones nobody
    ever checked. Returns ``(ApplyResult, VerifyResult)``.
    """
    if apply_fn is None:
        from vtex_fixed_price_uploader.runner import apply as apply_fn
    if verify_fn is None:
        from vtex_fixed_price_uploader.verify import verify as verify_fn

    result = apply_fn(config, pre, token, log, progress=progress,
                      abandon_unfinished=abandon_unfinished)
    for line in outcome_lines(result):
        say(line)
    # Said BEFORE verify_fn, because verify_fn sleeps first and prints
    # nothing. Two silent minutes read as a frozen tab, and the operator's
    # instinct - reload, or press Upload again - is the worst thing they can
    # do here.
    say(SETTLE_NOTICE)
    check = verify_fn(config, pre, token)
    say(verification_summary(check))
    return result, check


def verify_only(config, pre, token, say, verify_fn=None):
    """Read the rows back again without writing anything.

    This is how an operator settles an unreadable row, or checks a halted run
    later, without touching production again.
    """
    if verify_fn is None:
        from vtex_fixed_price_uploader.verify import verify as verify_fn
    say("{} Nothing is written.".format(SETTLE_NOTICE))
    check = verify_fn(config, pre, token)
    say(verification_summary(check))
    return check


def _restorable(snapshot, pair):
    """The prior policy-1 array for a pair, or None - restore's own rule."""
    from vtex_fixed_price_uploader.pricing import policy1
    from vtex_fixed_price_uploader.reader import is_failed_read

    record = (snapshot or {}).get(pair)
    if not record:
        return None
    status, data = record
    if is_failed_read(status) or data is None:
        return None
    return policy1(data) or None


def restore_preview(pairs, snapshot):
    """What an undo would put back, and for how many pairs - before it runs."""
    usable = sum(1 for pair in pairs if _restorable(snapshot, pair))
    total = len(pairs)
    missing = total - usable
    sentence = ("This will put back the prices that were in VTEX before the "
                "last upload, for {:,} of {:,} pairs.".format(usable, total))
    if missing:
        sentence += (" {:,} pairs have no usable saved copy and will be left "
                     "exactly as they are.".format(missing))
    return sentence + " Nothing is written until you confirm."


def run_restore(config, snapshot, pairs, token, log, say, progress=None,
                restore_fn=None):
    """Put the prior prices back and account for every pair."""
    if restore_fn is None:
        from vtex_fixed_price_uploader.restore import restore as restore_fn

    result = restore_fn(config, snapshot, pairs, token, log, progress=progress)
    if result.halted:
        say(_as_sentence(result.halted))
    say("Put back {:,} price{}.".format(
        result.restored, "" if result.restored == 1 else "s"))
    if result.failed:
        say("{:,} pairs could not be put back and are still holding the new "
            "prices. Run the undo again.".format(result.failed))
    left = len(pairs) - result.restored - result.failed
    if left > 0:
        say("{:,} pairs were left exactly as they are, because the saved copy "
            "could not answer for them.".format(left))
    return result


def build_ui(config, log_path, snapshot_path):
    """Assemble the operator's screen. Requires the ``notebook`` extra.

    The paths are kept in the returned dictionary and used by
    ``run_interactive`` - the notebook passes real Drive paths and they must
    be the ones the log and the snapshot actually use.
    """
    import ipywidgets as widgets
    from IPython.display import display

    upload = widgets.FileUpload(accept=".csv", multiple=False,
                                description="Choose CSV")
    token = widgets.Password(description="Login:",
                             placeholder="paste your VTEX login here")
    check_only = widgets.Checkbox(value=False,
                                  description="Check only - do not upload")
    abandon = widgets.Checkbox(
        value=False,
        description=("Abandon the unfinished upload log - rows already "
                     "written stay written in VTEX"))
    start = widgets.Button(description="Check this file",
                           button_style="primary")
    undo = widgets.Button(description="Put the previous prices back")
    where = widgets.Label(
        value="{} accounts configured: {}. Log: {}".format(
            len(config.accounts), ", ".join(sorted(config.accounts)),
            log_path))

    display(widgets.VBox([where, upload, token, check_only, abandon, start,
                          undo]))
    output = widgets.Output()
    display(output)
    return {"upload": upload, "token": token, "check_only": check_only,
            "abandon": abandon, "start": start, "undo": undo,
            "output": output, "config": config, "log_path": log_path,
            "snapshot_path": snapshot_path}


def run_interactive(config, ui, folder=None):
    """Wire the widgets to the pipeline. All optional imports stay lazy."""
    import ipywidgets as widgets
    from IPython.display import HTML, display

    from vtex_fixed_price_uploader.writelog import WriteLog

    log_path = ui.get("log_path") or (
        "{}/write-log.jsonl".format(folder) if folder else "")
    snapshot_path = ui.get("snapshot_path") or (
        "{}/snapshot.json".format(folder) if folder else "")
    if not log_path or not snapshot_path:
        raise ValueError(
            "run_interactive needs a ui built by build_ui, or a folder")

    log = WriteLog(log_path)
    out = ui["output"]

    def say(text):
        out.append_display_data(HTML(
            '<div style="font-family:sans-serif;font-size:14px">{}</div>'.format(
                text)))

    def excuse(exc):
        say(friendly_error(exc, ui["token"].value))

    def on_start(_button):
        out.clear_output()
        with out:
            try:
                start_run()
            except Exception as exc:                  # noqa: BLE001
                excuse(exc)

    def start_run():
        from vtex_fixed_price_uploader.reader import save_snapshot
        from vtex_fixed_price_uploader.report import (
            download_link_html, findings_csv, render_html, scope_sentence)
        from vtex_fixed_price_uploader.runner import preflight

        if not ui["upload"].value:
            say("Choose a CSV file first.")
            return
        if not ui["token"].value:
            say("Paste your VTEX login first.")
            return

        item = list(ui["upload"].value)[0]
        content = (item["content"] if isinstance(item, dict)
                   else ui["upload"].value[item]["content"])

        bar = widgets.IntProgress(min=0, max=100, description="Reading:")
        label = widgets.Label(value="")
        display(widgets.HBox([bar, label]))

        def progress(done, total):
            bar.max = total
            bar.value = done
            label.value = "{:,} / {:,}".format(done, total)

        import io
        pre = preflight(config, io.BytesIO(content),
                        ui["token"].value, progress=progress)
        save_snapshot(pre.reads, snapshot_path)
        display(HTML(render_html(pre.model)))

        if pre.findings:
            # Rule ids and per-region detail exist only here. Without the
            # download they are unreachable by any means.
            display(HTML(download_link_html(
                findings_csv(pre.findings, pre.names), "findings.csv",
                "Download every finding, with rule ids and regions (CSV)")))

        if ui["check_only"].value:
            say("Check only - nothing was uploaded.")
            return

        state = AckState(pre.model)
        groups = pre.model.warnings + pre.model.ending
        boxes = []
        for group in groups:
            box = widgets.Checkbox(value=False, indent=False,
                                   description="{} - {}".format(
                                       group.product, group.message[:70]))
            boxes.append(box)

        typed = widgets.Text(description="Confirm:", placeholder=str(
            pre.model.write_rows))
        go = widgets.Button(
            description="Upload {:,} prices".format(pre.model.write_rows),
            button_style="danger", disabled=True)
        # The scope belongs where the click happens, not in the verdict block
        # far above it, separated by every rendered item.
        scope = widgets.HTML(value=(
            '<div style="font-size:14px;font-weight:600;'
            'margin-top:10px">{}</div>'.format(scope_sentence(pre.model))))
        recheck = widgets.Button(description="Check what landed (no writing)")
        why = widgets.Label(value=state.blocking_reason)

        def refresh(_change=None):
            for box, group in zip(boxes, groups):
                state.tick(group.key, box.value)
            state.set_typed(typed.value)
            go.disabled = not state.ready
            why.value = state.blocking_reason

        for box in boxes:
            box.observe(refresh, names="value")
        typed.observe(refresh, names="value")

        def on_go(_button):
            go.disabled = True
            with out:
                try:
                    abandon = bool(ui["abandon"].value)
                    if abandon:
                        say("You chose to abandon the unfinished upload log. "
                            "Rows already written stay written in VTEX.")
                    apply_and_verify(config, pre, ui["token"].value, log, say,
                                     progress=progress,
                                     abandon_unfinished=abandon)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        def on_recheck(_button):
            with out:
                try:
                    verify_only(config, pre, ui["token"].value, say)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        go.on_click(on_go)
        recheck.on_click(on_recheck)

        controls = list(boxes)
        if pre.model.needs_typed_confirmation:
            controls.append(typed)
        controls.extend([scope, go, why, recheck])
        display(widgets.VBox(controls))
        refresh()

    def on_undo(_button):
        with out:
            try:
                offer_undo()
            except Exception as exc:                  # noqa: BLE001
                excuse(exc)

    def offer_undo():
        from vtex_fixed_price_uploader.reader import load_snapshot
        from vtex_fixed_price_uploader.restore import pairs_from_log

        if not ui["token"].value:
            say("Paste your VTEX login first, then press Put the previous prices back "
                "again.")
            return
        pairs = pairs_from_log(log_path)
        if not pairs:
            say("There is nothing to undo - this log records no successful "
                "writes.")
            return
        snapshot = load_snapshot(snapshot_path)
        say(restore_preview(pairs, snapshot))

        confirm = widgets.Button(description="Yes, put those prices back",
                                 button_style="danger")

        def on_confirm(_click):
            confirm.disabled = True
            with out:
                try:
                    run_restore(config, snapshot, pairs, ui["token"].value,
                                log, say)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        confirm.on_click(on_confirm)
        display(confirm)

    ui["start"].on_click(on_start)
    ui["undo"].on_click(on_undo)
