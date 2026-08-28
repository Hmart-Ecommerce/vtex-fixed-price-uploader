from vtex_fixed_price_uploader.compose import Composition
from vtex_fixed_price_uploader.report import (
    TYPED_CONFIRMATION_THRESHOLD, build_model, findings_csv, render_html)
from vtex_fixed_price_uploader.rules import Finding


def finding(rule="W1", severity="warning", sku="111", code="R1",
            account="acct_one", message="Your price is higher.", detail="d"):
    return Finding(rule=rule, severity=severity, sku=sku, code=code,
                   account=account, message=message, detail=detail)


def test_same_rule_and_sku_across_regions_collapses_to_one_group():
    findings = [finding(code="R1", account="acct_one"),
                finding(code="R2", account="acct_two")]
    model = build_model(findings, {}, set(), {})
    assert len(model.warnings) == 1
    assert model.warnings[0].codes == ("R1", "R2")


def test_different_rules_stay_separate():
    findings = [finding(rule="W1"), finding(rule="W3", message="List price differs")]
    model = build_model(findings, {}, set(), {})
    assert len(model.warnings) == 2


def test_blocking_findings_land_in_their_own_bucket():
    model = build_model([finding(rule="B1", severity="blocking")], {}, set(), {})
    assert len(model.blocking) == 1 and model.warnings == ()


def test_w6_lands_in_the_ending_bucket_not_warnings():
    model = build_model([finding(rule="W6")], {}, set(), {})
    assert len(model.ending) == 1
    assert model.warnings == ()


def test_w6_keys_are_still_acknowledged():
    model = build_model([finding(rule="W6")], {}, set(), {})
    assert model.ending[0].key in model.ack_keys


def test_ack_keys_cover_warnings_and_endings_only():
    findings = [finding(rule="W1"), finding(rule="W6"),
                finding(rule="B1", severity="blocking"),
                finding(rule="I1", severity="info")]
    model = build_model(findings, {}, set(), {})
    assert len(model.ack_keys) == 2


def test_product_name_is_used_when_known():
    model = build_model([finding()], {}, set(), {"111": "Widget 12oz"})
    assert model.warnings[0].product == "Widget 12oz"


def test_product_falls_back_to_the_sku():
    model = build_model([finding()], {}, set(), {})
    assert model.warnings[0].product == "SKU 111"


def test_counts_are_derived_from_pairs_and_compositions():
    comps = {("111", "acct_one"): Composition(
        new_array=[{"value": 1}], dropped=[{"value": 2}, {"value": 3}],
        kept=[])}
    model = build_model([], comps, {("111", "acct_one")}, {})
    assert model.write_rows == 1
    assert model.removed_entries == 2


def test_blocked_rows_counts_distinct_pairs_not_findings():
    findings = [finding(rule="B1", severity="blocking"),
                finding(rule="B5", severity="blocking")]
    model = build_model(findings, {}, set(), {})
    assert model.blocked_rows == 1


def test_typed_confirmation_off_below_the_threshold():
    comps = {("111", "acct_one"): Composition(new_array=[], dropped=[], kept=[])}
    model = build_model([], comps, {("111", "acct_one")}, {})
    assert model.needs_typed_confirmation is False


def test_typed_confirmation_on_above_the_threshold():
    pairs = {(str(i), "acct_one") for i in range(TYPED_CONFIRMATION_THRESHOLD + 1)}
    comps = {p: Composition(new_array=[{"value": 1}], dropped=[], kept=[])
             for p in pairs}
    model = build_model([], comps, pairs, {})
    assert model.needs_typed_confirmation is True


def test_group_keys_are_unique_and_stable():
    findings = [finding(rule="W1"), finding(rule="W3", message="x")]
    first = build_model(findings, {}, set(), {})
    second = build_model(findings, {}, set(), {})
    keys = [g.key for g in first.warnings]
    assert len(keys) == len(set(keys))
    assert keys == [g.key for g in second.warnings]


def test_render_html_contains_the_counts():
    comps = {("111", "acct_one"): Composition(
        new_array=[{"value": 1}], dropped=[{"value": 2}], kept=[])}
    html = render_html(build_model([finding()], comps,
                                   {("111", "acct_one")}, {"111": "Widget"}))
    assert "Widget" in html
    assert "1" in html


def test_render_html_never_leaks_rule_ids():
    findings = [finding(rule="W1"), finding(rule="B1", severity="blocking"),
                finding(rule="W6")]
    html = render_html(build_model(findings, {}, set(), {}))
    for rule in ("W1", "B1", "W6"):
        assert rule not in html


def test_render_html_escapes_product_names():
    model = build_model([finding()], {}, set(), {"111": "<script>x</script>"})
    assert "<script>" not in render_html(model)


def test_findings_csv_has_a_header_and_the_rule_ids():
    csv_text = findings_csv([finding(rule="W1")], {"111": "Widget"})
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("rule,severity,sku,product")
    assert "W1" in lines[1]
    assert "Widget" in lines[1]
