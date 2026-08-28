"""What the operator reads before deciding.

Verdict first, data second. Findings are grouped by (rule, sku) so one issue
hitting nine regions renders as one line with a region count, which is what
turns ~74 rows into ~15 readable items.

Rule ids never reach the screen. They belong in the downloadable CSV, which
the screen offers as a download so that detail is actually reachable.

A group names every region it stands for. Collapsing nine codes into "9
regions" destroys the one fact the fix instruction asks the operator to act on
- which region to remove from the file.
"""

import base64
import csv
import html
import io
from dataclasses import dataclass

TYPED_CONFIRMATION_THRESHOLD = 500

SECTION_TITLES = {
    "blocking": "Blocked - these will not be uploaded",
    "warning": "Please check before continuing",
    "ending": "Promotions that will end early",
    "info": "For your information",
}


# The screen paints its own colours instead of inheriting them. A notebook
# output cell can be light or dark depending on the operator's theme, and a
# verdict that reads clearly in one and washes out in the other is a verdict
# nobody trusts. Every surface below sets both its background and its ink.
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,sans-serif"
INK = "#16181d"
MUTED = "#5b6270"
LINE = "#e4e7ec"
PAPER = "#ffffff"

# (ink, fill) per tone. Zero counts deliberately render in the neutral tone:
# a red "0 blocked" pulls the eye to the one number that needs no attention.
TONE = {
    "ok": ("#0f7a43", "#e9f6ef"),
    "warn": ("#8a5a0b", "#fdf4e3"),
    "bad": ("#a32d2d", "#fbecec"),
    "info": ("#39414f", "#eef1f5"),
    "none": ("#7b8290", "#f4f5f7"),
}


def _tone(name):
    return TONE.get(name, TONE["info"])


def _shell(inner):
    """One wrapper so every block on the screen shares a width and a font."""
    return ('<div style="font-family:{font};max-width:820px;color:{ink};'
            'font-size:14px;line-height:1.55">{inner}</div>').format(
                font=FONT, ink=INK, inner=inner)


def _stat(label, value, tone):
    """One number, large, with its meaning under it."""
    ink, fill = _tone(tone if value else "none")
    return ('<div style="display:inline-block;vertical-align:top;'
            'background:{fill};border-radius:6px;padding:12px 16px;'
            'margin:0 8px 8px 0;min-width:132px">'
            '<div style="font-size:11px;letter-spacing:.06em;'
            'text-transform:uppercase;color:{ink};opacity:.85">{label}</div>'
            '<div style="font-size:26px;font-weight:600;color:{ink};'
            'margin-top:2px">{value:,}</div></div>').format(
                fill=fill, ink=ink, label=html.escape(label), value=value)


def _panel(body, tone="info"):
    ink, _fill = _tone(tone)
    return ('<div style="background:{paper};border:1px solid {line};'
            'border-left:4px solid {ink};border-radius:6px;padding:16px 20px;'
            'margin:10px 0">{body}</div>').format(
                paper=PAPER, line=LINE, ink=ink, body=body)


def _title(text):
    return ('<div style="font-size:17px;font-weight:600;margin-bottom:12px">'
            '{}</div>').format(html.escape(text))


def _note(text):
    return ('<div style="font-size:13px;color:{muted};margin-top:8px">{text}'
            '</div>').format(muted=MUTED, text=html.escape(text))


def ack_label_html(group):
    """The full text of one thing being acknowledged, beside its checkbox.

    This label was cut to 70 characters and then cut again by the widget's own
    width, so the operator ticked a box whose sentence they could not finish
    reading. Acknowledgement is a decision, and a decision has to be legible
    at the point it is made - including the fix instruction, so the row stands
    on its own without scrolling back up to the report.
    """
    return ('<div style="font-family:{font};color:{ink};font-size:14px;'
            'line-height:1.5;padding-top:2px">'
            '<strong>{product}</strong>'
            '<span style="color:{muted};font-size:12px;margin-left:8px">'
            '{badge}</span><br>{message}'
            '<span style="color:{muted}"> {detail}</span></div>').format(
                font=FONT, ink=INK, muted=MUTED,
                product=html.escape(group.product),
                badge=html.escape(_regions_label(group.codes)),
                message=html.escape(group.message),
                detail=html.escape(group.detail))


def render_notice(text, tone="info"):
    """A single paragraph that has to be read, not skimmed past."""
    return _shell(_panel(
        '<div style="font-size:14px">{}</div>'.format(html.escape(text)),
        tone))


def render_counts(title, stats, notes=(), tone="info"):
    """A headline, the numbers that matter, then the sentences under them.

    ``stats`` is a sequence of (label, value, tone). Used by every screen that
    reports an outcome, so the write, the read-back and the undo all read the
    same way instead of each inventing its own shape.
    """
    body = [_title(title),
            "".join(_stat(label, value, item_tone)
                    for label, value, item_tone in stats)]
    body.extend(_note(note) for note in notes if note)
    return _shell(_panel("".join(body), tone))


def render_result(title, lines, tone="info"):
    """A titled block of sentences - the undo and the halt paths."""
    body = [_title(title)]
    body.extend(
        '<div style="font-size:14px;margin-top:6px">{}</div>'.format(
            html.escape(line)) for line in lines if line)
    return _shell(_panel("".join(body), tone))


@dataclass(frozen=True)
class Group:
    key: str
    rule: str
    severity: str
    sku: str
    product: str
    codes: tuple
    message: str
    detail: str


@dataclass(frozen=True)
class ReportModel:
    write_rows: int = 0
    blocked_rows: int = 0
    warning_rows: int = 0
    removed_entries: int = 0
    blocking: tuple = ()
    warnings: tuple = ()
    ending: tuple = ()
    info: tuple = ()
    ack_keys: tuple = ()
    needs_typed_confirmation: bool = False
    total_rows: int = 0
    combined_rows: int = 0
    account_count: int = 0


def _product(sku, names):
    return names.get(str(sku)) or "SKU {}".format(sku)


def _group(findings, names):
    """Collapse findings into groups keyed by (rule, sku), order preserved."""
    buckets = {}
    for finding in findings:
        key = (finding.rule, finding.sku)
        if key not in buckets:
            buckets[key] = {"first": finding, "codes": []}
        if finding.code and finding.code not in buckets[key]["codes"]:
            buckets[key]["codes"].append(finding.code)
    groups = []
    for (rule, sku), bucket in buckets.items():
        first = bucket["first"]
        groups.append(Group(
            key="{}-{}".format(rule, sku),
            rule=rule,
            severity=first.severity,
            sku=sku,
            product=_product(sku, names),
            codes=tuple(bucket["codes"]),
            message=first.message,
            detail=first.detail,
        ))
    return tuple(groups)


def build_model(findings, compositions, write_pairs, names, total_rows=None):
    """Everything the screen and the confirmation need, and nothing else.

    `total_rows` is the number of rows the FILE carried, which the caller has
    and this function does not. It is not derivable here: `compositions` is
    keyed by (sku, account), so its length is a count of pairs, and a file may
    legally carry several rows for one pair - B1 only blocks duplicate rows
    whose windows OVERLAP. Passing the pair count off as the file total is
    what made the screen disagree with the operator's own spreadsheet.
    """
    blocking = _group([f for f in findings if f.severity == "blocking"], names)
    warnings = _group([f for f in findings
                       if f.severity == "warning" and f.rule != "W6"], names)
    ending = _group([f for f in findings if f.rule == "W6"], names)
    info = _group([f for f in findings if f.severity == "info"], names)

    write_rows = sum(1 for pair in write_pairs if pair in compositions)
    removed = sum(len(compositions[pair].dropped) for pair in write_pairs
                  if pair in compositions)
    warning_rows = len({(f.sku, f.account) for f in findings
                        if f.severity == "warning"})
    blocked_rows = len({(f.sku, f.account) for f in findings
                        if f.severity == "blocking"})

    ack_keys = tuple(g.key for g in warnings + ending)
    # The blast radius is what one click does, so the threshold weighs the
    # writes and the removals together. Judging each figure alone let 369
    # writes plus 432 removals through with no typed step.
    typed = (write_rows + removed) > TYPED_CONFIRMATION_THRESHOLD

    # The denominator the headline numbers are read against. Without a real
    # count from the caller, fall back to the pair count - which is exact
    # whenever no two rows shared a pair, and is all a direct caller has.
    if total_rows is None:
        total_rows = len(compositions)
    # write_rows + blocked_rows accounts for every PAIR. Anything left over is
    # rows that shared a (sku, account) with another row and were merged into
    # one payload. Naming it is the only way the equation can add up; dropping
    # it would leave the operator to discover the gap themselves.
    combined_rows = total_rows - write_rows - blocked_rows
    account_count = len({account for pair in write_pairs
                         if pair in compositions
                         for account in (pair[1],)})

    return ReportModel(
        write_rows=write_rows, blocked_rows=blocked_rows,
        warning_rows=warning_rows, removed_entries=removed,
        blocking=blocking, warnings=warnings, ending=ending, info=info,
        ack_keys=ack_keys, needs_typed_confirmation=typed,
        total_rows=total_rows, combined_rows=combined_rows,
        account_count=account_count)


def _regions_label(codes):
    """Name every region in the group, however many there are.

    The fix instruction for these findings is "remove this region from your
    file", so the codes are the actionable part. A long line is a smaller
    problem than an unanswerable one.
    """
    return ", ".join(codes)


def _section_html(title, groups, tone):
    if not groups:
        return ""
    ink, fill = _tone(tone)
    rows = []
    for group in groups:
        badge = _regions_label(group.codes)
        rows.append(
            '<div style="border-top:1px solid {line};padding:11px 0">'
            '<div style="font-weight:600;font-size:14px">{product}'
            '<span style="font-weight:400;font-size:12px;color:{muted};'
            'margin-left:8px">{badge}</span></div>'
            '<div style="font-size:13px;color:{muted};line-height:1.6;'
            'margin-top:2px">{message} {detail}</div></div>'.format(
                line=LINE, muted=MUTED,
                product=html.escape(group.product),
                badge=html.escape(badge),
                message=html.escape(group.message),
                detail=html.escape(group.detail)))
    return (
        '<div style="background:{paper};border:1px solid {line};'
        'border-left:4px solid {ink};border-radius:6px;padding:14px 18px;'
        'margin:10px 0">'
        '<div style="display:inline-block;background:{fill};color:{ink};'
        'font-weight:600;font-size:14px;padding:3px 10px;border-radius:4px">'
        '{title}</div>'
        '<div style="font-size:12px;color:{muted};margin:6px 0 2px">{count} '
        'item{plural}</div>{rows}</div>'.format(
            paper=PAPER, line=LINE, ink=ink, fill=fill, muted=MUTED,
            title=html.escape(title), count=len(groups),
            plural="" if len(groups) == 1 else "s", rows="".join(rows)))


def reconciliation(model):
    """The equation, with every term it actually needs to balance.

    Written and blocked are counts of (sku, account) pairs; the file total is
    a count of rows. They differ by exactly the rows that shared a pair with
    another row, so that third term is stated whenever it is not zero rather
    than left for the operator to find as an unexplained gap.
    """
    tail = "= {total:,} price rows in the file.".format(total=model.total_rows)
    head = "{write:,} written + {blocked:,} blocked".format(
        write=model.write_rows, blocked=model.blocked_rows)
    if model.combined_rows < 0:
        # Only reachable when a caller supplies no file total and the fallback
        # pair count lands under written + blocked. Printing "+ -2 combined"
        # would render an equation that balances on a negative number of rows;
        # saying the total is unknown is the only honest thing left.
        return ("{} rows. The file total could not be reconciled - open the "
                "findings CSV for the full list.".format(head))
    if not model.combined_rows:
        return "{} {}".format(head, tail)
    return ("{} + {:,} combined with another row for the same SKU and "
            "account {}".format(head, model.combined_rows, tail))


def headline(model):
    """The one-line verdict.

    "Ready to upload" is only true when nothing was excluded. Saying it over
    171 blocked rows tells the operator the file is fine when it is not.
    """
    if not model.write_rows:
        return "Nothing to upload"
    if model.blocked_rows:
        return "Part of this file cannot be uploaded"
    return "Ready to upload"


def scope_sentence(model):
    """The full blast radius of one click, in one sentence.

    Rows, accounts and removals together. The removal count used to sit in the
    verdict block far above the button, separated by every rendered item.
    """
    prices = "{:,} price{}".format(model.write_rows,
                                   "" if model.write_rows == 1 else "s")
    accounts = "{:,} account{}".format(model.account_count,
                                       "" if model.account_count == 1 else "s")
    if not model.removed_entries:
        return ("This will write {} to {}. No existing prices will be "
                "removed.".format(prices, accounts))
    return ("This will write {} to {} and remove {:,} existing price{} that "
            "overlap the new dates.".format(
                prices, accounts, model.removed_entries,
                "" if model.removed_entries == 1 else "s"))


def download_link_html(text, filename, label=None):
    """A self-contained download link for a text file.

    The bytes travel in the href, so the link keeps working in a notebook
    output cell that has no server behind it.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return ('<a download="{filename}" href="data:text/csv;base64,{payload}" '
            'style="font-family:sans-serif;font-size:13px">{label}</a>'.format(
                filename=html.escape(filename), payload=payload,
                label=html.escape(label or filename)))


def render_html(model):
    """The operator's screen. Static content only - controls are widgets.

    The three numbers lead, because they are the three the operator acts on:
    what goes up, what needs a decision, what will not go at all. The
    sentences under them stay, because a number without its reconciliation is
    how a partial upload gets mistaken for a whole one.
    """
    blocked_tone = "bad" if model.blocked_rows else "none"
    if not model.write_rows:
        head_tone = "bad"
    elif model.blocked_rows or model.warning_rows:
        head_tone = "warn"
    else:
        head_tone = "ok"

    stats = (("Will be written", model.write_rows, "ok"),
             ("Need your check", model.warning_rows, "warn"),
             ("Blocked", model.blocked_rows, blocked_tone))

    body = [
        _title(headline(model)),
        "".join(_stat(label, value, tone) for label, value, tone in stats),
        '<div style="font-size:14px;margin-top:6px">'
        '<strong>{total:,}</strong> price rows in your file, one for each SKU '
        'and region you priced</div>'.format(total=model.total_rows),
        _note(reconciliation(model)),
        '<div style="font-size:13px;color:{muted};margin-top:8px">'
        '<strong>{attention:,}</strong> rows need your attention. This number '
        'overlaps both numbers above - blocked rows raise warnings too - so '
        'it is not a third group and does not add to the total.</div>'.format(
            muted=MUTED, attention=model.warning_rows),
    ]
    if model.removed_entries:
        body.append(_note(
            "This will also remove {:,} existing prices that overlap the new "
            "dates.".format(model.removed_entries)))

    parts = [_panel("".join(body), head_tone),
             _section_html(SECTION_TITLES["blocking"], model.blocking, "bad"),
             _section_html(SECTION_TITLES["warning"], model.warnings, "warn"),
             _section_html(SECTION_TITLES["ending"], model.ending, "warn"),
             _section_html(SECTION_TITLES["info"], model.info, "none")]
    return _shell("".join(parts))


def findings_csv(findings, names):
    """The downloadable detail, rule ids included."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["rule", "severity", "sku", "product", "region",
                     "account", "message", "detail"])
    for finding in findings:
        writer.writerow([finding.rule, finding.severity, finding.sku,
                         _product(finding.sku, names), finding.code,
                         finding.account, finding.message, finding.detail])
    return buffer.getvalue()
