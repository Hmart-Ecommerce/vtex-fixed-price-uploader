"""What the operator reads before deciding.

Verdict first, data second. Findings are grouped by (rule, sku) so one issue
hitting nine regions renders as one line with a region count, which is what
turns ~74 rows into ~15 readable items.

Rule ids never reach the screen. They belong in the downloadable CSV, where
someone debugging can find them.
"""

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


def build_model(findings, compositions, write_pairs, names):
    """Everything the screen and the confirmation need, and nothing else."""
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
    typed = (write_rows > TYPED_CONFIRMATION_THRESHOLD
             or removed > TYPED_CONFIRMATION_THRESHOLD)

    return ReportModel(
        write_rows=write_rows, blocked_rows=blocked_rows,
        warning_rows=warning_rows, removed_entries=removed,
        blocking=blocking, warnings=warnings, ending=ending, info=info,
        ack_keys=ack_keys, needs_typed_confirmation=typed)


def _regions_label(codes):
    if not codes:
        return ""
    if len(codes) == 1:
        return codes[0]
    return "{} regions".format(len(codes))


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


def render_html(model):
    """The operator's screen. Static content only - controls are widgets."""
    headline = "Ready to upload" if model.write_rows else "Nothing to upload"
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'max-width:820px">',
        '<div style="border:1px solid #d8d8d8;padding:16px 20px">'
        '<div style="font-size:18px;font-weight:600;margin-bottom:10px">'
        '{}</div>'.format(html.escape(headline)),
        '<div style="font-size:14px;line-height:1.9">'
        '<strong>{:,}</strong> prices will be written<br>'
        '<strong>{:,}</strong> rows need your attention<br>'
        '<strong>{:,}</strong> rows blocked and excluded</div>'.format(
            model.write_rows, model.warning_rows, model.blocked_rows),
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
