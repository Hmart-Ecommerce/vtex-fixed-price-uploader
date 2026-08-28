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


def _section_html(title, groups, colour):
    if not groups:
        return ""
    rows = []
    for group in groups:
        badge = _regions_label(group.codes)
        rows.append(
            '<div style="border-top:1px solid #e6e6e6;padding:10px 0">'
            '<div style="font-weight:600;font-size:14px">{product}'
            '<span style="font-weight:400;font-size:12px;color:#666;'
            'margin-left:8px">{badge}</span></div>'
            '<div style="font-size:13px;color:#444;line-height:1.55">'
            '{message} {detail}</div></div>'.format(
                product=html.escape(group.product),
                badge=html.escape(badge),
                message=html.escape(group.message),
                detail=html.escape(group.detail)))
    return (
        '<div style="border:1px solid #e0e0e0;border-left:3px solid {colour};'
        'padding:14px 18px;margin:12px 0">'
        '<div style="font-weight:600;font-size:15px">{title}</div>'
        '<div style="font-size:12px;color:#777;margin-bottom:6px">{count} '
        'item{plural}</div>{rows}</div>'.format(
            colour=colour, title=html.escape(title), count=len(groups),
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
    """The operator's screen. Static content only - controls are widgets."""
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:820px">',
        '<div style="border:1px solid #d8d8d8;padding:16px 20px">'
        '<div style="font-size:18px;font-weight:600;margin-bottom:10px">'
        '{}</div>'.format(html.escape(headline(model))),
        '<div style="font-size:14px;line-height:1.9">'
        '<strong>{total:,}</strong> price rows in your file, one for each SKU '
        'and region you priced<br>'
        '<strong>{write:,}</strong> prices will be written<br>'
        '<strong>{blocked:,}</strong> rows blocked and excluded</div>'.format(
            total=model.total_rows, write=model.write_rows,
            blocked=model.blocked_rows),
        '<div style="font-size:13px;color:#555;margin-top:8px">{}</div>'
        .format(html.escape(reconciliation(model))),
        '<div style="font-size:13px;color:#555;margin-top:8px">'
        '<strong>{attention:,}</strong> rows need your attention. This number '
        'overlaps both numbers above - blocked rows raise warnings too - so '
        'it is not a third group and does not add to the total.</div>'.format(
            attention=model.warning_rows),
    ]
    if model.removed_entries:
        parts.append(
            '<div style="font-size:13px;color:#555;margin-top:10px">'
            'This will also remove {:,} existing prices that overlap the new '
            'dates.</div>'.format(model.removed_entries))
    parts.append("</div>")
    parts.append(_section_html(SECTION_TITLES["blocking"], model.blocking,
                               "#a32d2d"))
    parts.append(_section_html(SECTION_TITLES["warning"], model.warnings,
                               "#ba7517"))
    parts.append(_section_html(SECTION_TITLES["ending"], model.ending,
                               "#ba7517"))
    parts.append(_section_html(SECTION_TITLES["info"], model.info, "#888"))
    parts.append("</div>")
    return "".join(parts)


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
