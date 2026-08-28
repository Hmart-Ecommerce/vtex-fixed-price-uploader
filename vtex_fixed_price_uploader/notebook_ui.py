"""Acknowledgement state and the optional notebook interface.

The readiness logic lives in ``AckState`` so the production-write gate can be
tested without notebook dependencies. The optional widgets are imported only
inside the functions that use them.
"""


class AckState:
    """Track the one acknowledgement of the warnings, and typed confirmation.

    Every warning group used to need its own tick. Seven checkboxes for seven
    facts already stated in full in the report above is seven chances to tick
    without reading, and it buried the upload button under a column of them.
    Reviewing the warnings is one decision, so it takes one confirmation. The
    report still carries every item, in full, with its fix instruction.
    """

    def __init__(self, model):
        self.model = model
        self.warning_groups = len(model.ack_keys or ())
        self._acknowledged = False
        self._typed = ""

    def acknowledge(self, done=True):
        self._acknowledged = bool(done)

    @property
    def _ack_ok(self):
        return self._acknowledged or not self.warning_groups

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
        return self._ack_ok and self._typed_ok

    @property
    def blocking_reason(self):
        if not self.model.write_rows:
            return "There is nothing to upload."
        if not self._ack_ok:
            return ("Read the {} item{} above, then confirm you have "
                    "reviewed them.".format(
                        self.warning_groups,
                        "" if self.warning_groups == 1 else "s"))
        if not self._typed_ok:
            return "Type {} to confirm.".format(self.model.write_rows)
        return ""


def verification_counts(check):
    """Assign every read-back row to exactly one bucket.

    ``still_multiple`` is an additional counter in ``VerifyResult`` and can
    overlap its matched or mismatched totals. For the visible accounting, each
    row is assigned exactly once, with several live prices taking precedence.

    The sentence and the on-screen counters both read from here, so the number
    in the card can never disagree with the number in the sentence beside it.
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

    return counts


def verification_summary(check):
    """Return an operator-facing, exhaustive read-back accounting."""
    counts = verification_counts(check)
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


def _emit(text, tone, say, show):
    """One sentence: plain when there is no screen, styled when there is.

    ``show`` is optional on purpose. Callers that only collect sentences -
    the tests, and any future non-notebook front end - keep getting plain
    text, and the styling stays a property of the screen rather than of the
    pipeline.
    """
    if show is None:
        say(text)
        return
    from vtex_fixed_price_uploader.report import render_notice
    show(render_notice(text, tone))


def _emit_write(result, say, show):
    """The write outcome: three counters, then every sentence they need."""
    lines = outcome_lines(result)
    if show is None:
        for line in lines:
            say(line)
        return
    from vtex_fixed_price_uploader.report import render_counts
    if result.halted:
        title, tone = "Upload stopped early", "bad"
    elif result.failed:
        title, tone = "Upload finished with failures", "bad"
    else:
        title, tone = "Upload finished", "ok"
    stats = (("Written", result.written, "ok"),
             ("Failed", result.failed, "bad"),
             ("Skipped", result.skipped, "info"))
    show(render_counts(title, stats, notes=lines, tone=tone))


def _emit_verification(check, say, show):
    """The read-back: the screen an operator uses to decide it really landed.

    Anything short of every attempted row matching is rendered as a problem,
    including a run with nothing to check. "0 attempted" in green would read
    as success to someone skimming.
    """
    text = verification_summary(check)
    if show is None:
        say(text)
        return
    from vtex_fixed_price_uploader.report import render_counts
    counts = verification_counts(check)
    attempted = len(check.rows)
    clean = attempted > 0 and counts["matched"] == attempted
    stats = (("Attempted", attempted, "info"),
             ("Matched", counts["matched"], "ok"),
             ("Mismatched", counts["mismatched"], "bad"),
             ("Still multiple", counts["still_multiple"], "warn"),
             ("Confirmed empty", counts["confirmed_empty"], "bad"),
             ("Unreadable", counts["unreadable"], "warn"))
    title = ("Every row confirmed in VTEX" if clean
             else "Read-back finished - some rows need attention")
    show(render_counts(title, stats, notes=(text,),
                       tone="ok" if clean else "bad"))


def apply_and_verify(config, pre, token, log, say, progress=None,
                     abandon_unfinished=False, apply_fn=None, verify_fn=None,
                     show=None):
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
    _emit_write(result, say, show)
    # Said BEFORE verify_fn, because verify_fn sleeps first and prints
    # nothing. Two silent minutes read as a frozen tab, and the operator's
    # instinct - reload, or press Upload again - is the worst thing they can
    # do here.
    _emit(SETTLE_NOTICE, "info", say, show)
    check = verify_fn(config, pre, token)
    _emit_verification(check, say, show)
    return result, check


def verify_only(config, pre, token, say, verify_fn=None, show=None):
    """Read the rows back again without writing anything.

    This is how an operator settles an unreadable row, or checks a halted run
    later, without touching production again.
    """
    if verify_fn is None:
        from vtex_fixed_price_uploader.verify import verify as verify_fn
    _emit("{} Nothing is written.".format(SETTLE_NOTICE), "info", say, show)
    check = verify_fn(config, pre, token)
    _emit_verification(check, say, show)
    return check


def _pairs(count):
    """"1 pair" or "18 pairs" - the undo's counts are read, not parsed."""
    return "{:,} pair{}".format(count, "" if count == 1 else "s")


def _restorable(snapshot, pair):
    """The prior policy-1 array for a pair, or None - restore's own rule.

    Mirrors `restore` exactly, including the distinction that matters: an
    empty list is a real answer ("this sku had no fixed price") and None is
    the absence of one. Returning `policy1(data) or None` collapsed those two,
    which is how the preview came to under-count what an undo would put back.
    """
    from vtex_fixed_price_uploader.pricing import policy1

    record = (snapshot or {}).get(pair)
    if not record:
        return None
    status, data = record
    if status != 200 or data is None:
        return None
    return policy1(data)


def restore_preview(pairs, snapshot):
    """What an undo would put back, and for how many pairs - before it runs."""
    usable = sum(1 for pair in pairs
                 if _restorable(snapshot, pair) is not None)
    total = len(pairs)
    missing = total - usable
    sentence = ("This will put back what was in VTEX before the last upload, "
                "for {:,} of {:,} pairs. For some of them that may mean "
                "removing a price the upload added, because there was no "
                "fixed price there before.".format(usable, total))
    if missing:
        sentence += (" {:,} pairs have no usable saved copy. They will be left "
                     "exactly as they are, still holding the prices the upload "
                     "wrote.".format(missing))
    return sentence + " Nothing is written until you confirm."


def run_restore(config, snapshot, pairs, token, log, say, progress=None,
                restore_fn=None, show=None):
    """Put the prior prices back and account for every pair."""
    if restore_fn is None:
        from vtex_fixed_price_uploader.restore import restore as restore_fn

    result = restore_fn(config, snapshot, pairs, token, log, progress=progress)
    lines = []
    if result.halted:
        lines.append(_as_sentence(result.halted))
    lines.append("Put back the previous state for {}.".format(
        _pairs(result.restored)))
    if result.failed:
        lines.append("{} could not be put back and are still holding the new "
                     "prices. Run the undo again.".format(
                         _pairs(result.failed)))
    # Every sentence below says what the pair means for production, not only
    # how it was counted. "restored 0, skipped 18" read as "the undo did
    # nothing"; the 18 new prices were in fact all still live.
    if result.skipped:
        lines.append("{} skipped - nothing was put back there, and the prices "
                     "the upload wrote are still live in VTEX. The saved copy "
                     "could not say what was there before, so changing "
                     "anything would have been a guess. Put those right by "
                     "hand or from a good snapshot.".format(
                         _pairs(result.skipped)))
    unreached = (len(pairs) - result.restored - result.failed
                 - result.skipped)
    if unreached > 0:
        lines.append("{} not reached because the undo stopped early - still "
                     "holding the prices the upload wrote.".format(
                         _pairs(unreached)))

    if show is None:
        for line in lines:
            say(line)
        return result

    from vtex_fixed_price_uploader.report import render_counts
    clean = not (result.halted or result.failed or result.skipped or unreached)
    stats = (("Put back", result.restored, "ok"),
             ("Failed", result.failed, "bad"),
             ("Skipped", result.skipped, "warn"),
             ("Not reached", max(unreached, 0), "warn"))
    show(render_counts(
        "Undo finished" if clean else "Undo finished - read this",
        stats, notes=lines, tone="ok" if clean else "bad"))
    return result


def build_ui(config, log_path, snapshot_path):
    """Assemble the operator's screen. Requires the ``notebook`` extra.

    The paths are kept in the returned dictionary and used by
    ``run_interactive`` - the notebook passes real Drive paths and they must
    be the ones the log and the snapshot actually use.
    """
    import ipywidgets as widgets
    from IPython.display import display

    wide = widgets.Layout(width="auto")
    upload = widgets.FileUpload(accept=".csv", multiple=False,
                                description="Choose CSV", layout=wide)
    token = widgets.Password(description="Login:",
                             placeholder="paste your VTEX login here")
    # Widget descriptions are cut to the widget's width by default, which is
    # how "Abandon the unfinished upload log" became "Abandon the unfinished
    # uploa...". An automatic width lets the label decide.
    check_only = widgets.Checkbox(value=False, indent=False, layout=wide,
                                  description="Check only - do not upload")
    abandon = widgets.Checkbox(
        value=False, indent=False, layout=wide,
        description=("Abandon the unfinished upload log - rows already "
                     "written stay written in VTEX"))
    start = widgets.Button(description="Check this file",
                           button_style="primary", layout=wide)
    undo = widgets.Button(description="Put the previous prices back",
                          layout=wide)
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

    def show(markup):
        """Every block the operator reads goes through here, already styled."""
        out.append_display_data(HTML(markup))

    def say(text, tone="info"):
        from vtex_fixed_price_uploader.report import render_notice
        show(render_notice(text, tone))

    def excuse(exc):
        # An error is the one message that must not look like the others.
        say(friendly_error(exc, ui["token"].value), "bad")

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
            say("Choose a CSV file first.", "warn")
            return
        if not ui["token"].value:
            say("Paste your VTEX login first.", "warn")
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
            say("Check only - nothing was uploaded.", "info")
            return

        state = AckState(pre.model)
        groups = pre.model.warnings + pre.model.ending
        # One confirmation, not one per group. The items themselves are in
        # the report above, in full - a widget label truncates and a column of
        # checkboxes pushed the upload button off the screen.
        reviewed = widgets.Button(
            description="I have reviewed the {} item{} above".format(
                len(groups), "" if len(groups) == 1 else "s"),
            button_style="primary",
            layout=widgets.Layout(width="auto"))

        typed = widgets.Text(description="Confirm:", placeholder=str(
            pre.model.write_rows))
        go = widgets.Button(
            description="Upload {:,} prices".format(pre.model.write_rows),
            button_style="danger", disabled=True,
            layout=widgets.Layout(width="auto"))
        # The scope belongs where the click happens, not in the verdict block
        # far above it, separated by every rendered item.
        scope = widgets.HTML(value=(
            '<div style="font-size:14px;font-weight:600;'
            'margin-top:10px">{}</div>'.format(scope_sentence(pre.model))))
        recheck = widgets.Button(description="Check what landed (no writing)",
                                 layout=widgets.Layout(width="auto"))
        why = widgets.Label(value=state.blocking_reason)

        def refresh(_change=None):
            state.set_typed(typed.value)
            go.disabled = not state.ready
            why.value = state.blocking_reason

        def on_reviewed(_button):
            state.acknowledge(True)
            reviewed.disabled = True
            reviewed.button_style = "success"
            reviewed.description = "Reviewed - upload unlocked"
            refresh()

        reviewed.on_click(on_reviewed)
        typed.observe(refresh, names="value")

        def on_go(_button):
            go.disabled = True
            with out:
                try:
                    abandon = bool(ui["abandon"].value)
                    if abandon:
                        say("You chose to abandon the unfinished upload "
                            "log. Rows already written stay written in "
                            "VTEX.", "warn")
                    apply_and_verify(config, pre, ui["token"].value, log,
                                     say, progress=progress,
                                     abandon_unfinished=abandon, show=show)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        def on_recheck(_button):
            with out:
                try:
                    verify_only(config, pre, ui["token"].value, say, show=show)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        go.on_click(on_go)
        recheck.on_click(on_recheck)

        controls = [reviewed] if groups else []
        if pre.model.needs_typed_confirmation:
            controls.append(typed)
        controls.extend([scope, go, why, recheck])
        display(widgets.VBox(controls))
        refresh()

    def on_undo(_button):
        # The undo used to be offered into the same output the upload screen
        # was still holding, so "Upload N prices", "Check what landed" and
        # "Yes, put those prices back" sat on screen together with nothing
        # saying which step each belonged to. Clearing first means the screen
        # shows one decision at a time.
        out.clear_output()
        with out:
            try:
                offer_undo()
            except Exception as exc:                  # noqa: BLE001
                excuse(exc)

    def offer_undo():
        from vtex_fixed_price_uploader.reader import load_snapshot
        from vtex_fixed_price_uploader.restore import pairs_from_log

        if not ui["token"].value:
            say("Paste your VTEX login first, then press Put the previous "
                "prices back again.", "warn")
            return
        pairs = pairs_from_log(log_path)
        if not pairs:
            say("There is nothing to undo - this log records no successful "
                "writes.", "info")
            return
        snapshot = load_snapshot(snapshot_path)
        say(restore_preview(pairs, snapshot), "warn")

        wide = widgets.Layout(width="auto")
        confirm = widgets.Button(description="Yes, put those prices back",
                                 button_style="danger", layout=wide)
        cancel = widgets.Button(
            description="No, leave the prices as they are", layout=wide)

        def settle():
            """An undo offer is answered once. Both buttons then stand down."""
            confirm.disabled = True
            cancel.disabled = True

        def on_confirm(_click):
            settle()
            with out:
                try:
                    run_restore(config, snapshot, pairs, ui["token"].value,
                                log, say, show=show)
                except Exception as exc:              # noqa: BLE001
                    excuse(exc)

        def on_cancel(_click):
            settle()
            with out:
                say("Nothing was put back. The prices from the last upload "
                    "are still live in VTEX.", "info")

        confirm.on_click(on_confirm)
        cancel.on_click(on_cancel)
        display(widgets.HBox([confirm, cancel]))

    ui["start"].on_click(on_start)
    ui["undo"].on_click(on_undo)
