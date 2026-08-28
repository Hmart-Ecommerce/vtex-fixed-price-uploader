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


def build_ui(config, log_path, snapshot_path):
    """Assemble the operator's screen. Requires the ``notebook`` extra."""
    import ipywidgets as widgets
    from IPython.display import display

    upload = widgets.FileUpload(accept=".csv", multiple=False,
                                description="Choose CSV")
    token = widgets.Password(description="Login:",
                             placeholder="paste your VTEX login here")
    check_only = widgets.Checkbox(value=False,
                                  description="Check only - do not upload")
    start = widgets.Button(description="Check this file",
                           button_style="primary")
    output = widgets.Output()

    display(widgets.VBox([upload, token, check_only, start, output]))
    return {"upload": upload, "token": token, "check_only": check_only,
            "start": start, "output": output}


def run_interactive(config, ui, folder):
    """Wire the widgets to the pipeline. All optional imports stay lazy."""
    import ipywidgets as widgets
    from IPython.display import HTML, display

    from vtex_fixed_price_uploader.reader import save_snapshot
    from vtex_fixed_price_uploader.report import render_html
    from vtex_fixed_price_uploader.runner import apply, preflight
    from vtex_fixed_price_uploader.verify import verify
    from vtex_fixed_price_uploader.writelog import WriteLog

    log = WriteLog("{}/write-log.jsonl".format(folder))
    out = ui["output"]

    def say(text):
        out.append_display_data(HTML(
            '<div style="font-family:sans-serif;font-size:14px">{}</div>'.format(
                text)))

    def on_start(_button):
        out.clear_output()
        with out:
            if not ui["upload"].value:
                say("Choose a CSV file first.")
                return
            if not ui["token"].value:
                say("Paste your VTEX login first.")
                return

            item = list(ui["upload"].value)[0]
            content = (item["content"] if isinstance(item, dict)
                       else ui["upload"].value[item]["content"])

            bar = widgets.IntProgress(min=0, max=100,
                                      description="Reading:")
            label = widgets.Label(value="")
            display(widgets.HBox([bar, label]))

            def progress(done, total):
                bar.max = total
                bar.value = done
                label.value = "{:,} / {:,}".format(done, total)

            import io
            pre = preflight(config, io.BytesIO(content),
                            ui["token"].value, progress=progress)
            save_snapshot(pre.reads, "{}/snapshot.json".format(folder))
            display(HTML(render_html(pre.model)))

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

            controls = list(boxes)
            if pre.model.needs_typed_confirmation:
                controls.append(typed)
            controls.extend([go, why])
            display(widgets.VBox(controls))

            def on_go(_button):
                go.disabled = True
                with out:
                    result = apply(config, pre, ui["token"].value, log,
                                   progress=progress)
                    if result.halted:
                        say(result.halted)
                        return
                    say("Wrote {:,} prices. Checking them now - this takes "
                        "about two minutes.".format(result.written))
                    check = verify(config, pre, ui["token"].value)
                    say(verification_summary(check))

            go.on_click(on_go)
            refresh()

    ui["start"].on_click(on_start)
