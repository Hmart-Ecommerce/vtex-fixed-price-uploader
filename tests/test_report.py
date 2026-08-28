from vtex_fixed_price_uploader.compose import Composition
from vtex_fixed_price_uploader.report import (
    TYPED_CONFIRMATION_THRESHOLD, build_model, download_link_html,
    findings_csv, render_html, scope_sentence)
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


# --- Fix wave D ---------------------------------------------------------


def comp(new=1, dropped=0):
    return Composition(new_array=[{"value": 1}] * new,
                       dropped=[{"value": 2}] * dropped, kept=[])


def batch(write=1, removed=0, accounts=("acct_one",), blocked_findings=()):
    """A model with `write` writable pairs spread over `accounts`."""
    comps, pairs = {}, set()
    for index in range(write):
        pair = (str(1000 + index), accounts[index % len(accounts)])
        comps[pair] = comp(new=1, dropped=(removed if index == 0 else 0))
        pairs.add(pair)
    return build_model(list(blocked_findings), comps, pairs, {})


def test_region_badge_names_every_region_it_stands_for():
    findings = [finding(code="R1"), finding(code="R2"), finding(code="R3")]
    model = build_model(findings, {}, set(), {})
    assert model.warnings[0].codes == ("R1", "R2", "R3")
    assert "R1, R2, R3" in render_html(model)


def test_region_badge_never_collapses_to_a_bare_count():
    findings = [finding(code="R{}".format(i)) for i in range(1, 10)]
    html = render_html(build_model(findings, {}, set(), {}))
    assert "9 regions" not in html
    for code in ("R1", "R5", "R9"):
        assert code in html


def test_model_carries_the_file_total_and_the_account_count():
    model = batch(write=4, accounts=("acct_one", "acct_two"))
    assert model.total_rows == 4
    assert model.account_count == 2


def test_render_html_states_the_file_total():
    model = build_model([finding(rule="B1", severity="blocking", sku="9")],
                        {("1", "acct_one"): comp(), ("9", "acct_one"): comp()},
                        {("1", "acct_one")}, {})
    html = render_html(model)
    assert model.total_rows == 2
    assert "rows in your file" in html


def test_render_html_reconciles_written_plus_blocked_to_the_total():
    model = build_model([finding(rule="B1", severity="blocking", sku="9")],
                        {("1", "acct_one"): comp(), ("9", "acct_one"): comp()},
                        {("1", "acct_one")}, {}, total_rows=2)
    assert ("1 written + 1 blocked = 2 price rows in the file."
            in render_html(model))


def test_render_html_says_the_attention_count_overlaps_the_others():
    model = build_model([finding(sku="1"), finding(rule="B1",
                                                   severity="blocking",
                                                   sku="9")],
                        {("1", "acct_one"): comp(), ("9", "acct_one"): comp()},
                        {("1", "acct_one")}, {})
    html = render_html(model).lower()
    assert "overlap" in html


def test_headline_does_not_claim_ready_when_rows_are_blocked():
    model = build_model([finding(rule="B1", severity="blocking", sku="9")],
                        {("1", "acct_one"): comp(), ("9", "acct_one"): comp()},
                        {("1", "acct_one")}, {})
    assert "Ready to upload" not in render_html(model)


def test_headline_still_says_ready_when_nothing_is_blocked():
    assert "Ready to upload" in render_html(batch(write=2))


def test_typed_confirmation_counts_writes_and_removals_together():
    half = TYPED_CONFIRMATION_THRESHOLD // 2 + 1
    model = batch(write=half, removed=half)
    assert model.write_rows <= TYPED_CONFIRMATION_THRESHOLD
    assert model.removed_entries <= TYPED_CONFIRMATION_THRESHOLD
    assert model.needs_typed_confirmation is True


def test_scope_sentence_names_rows_accounts_and_removals():
    sentence = scope_sentence(batch(write=2, removed=3,
                                    accounts=("acct_one", "acct_two")))
    assert "2 prices" in sentence
    assert "2 accounts" in sentence
    assert "3 existing prices" in sentence


def test_scope_sentence_is_explicit_when_nothing_is_removed():
    sentence = scope_sentence(batch(write=2))
    assert "no existing prices" in sentence.lower()


def test_download_link_carries_the_csv_and_a_filename():
    import base64
    csv_text = findings_csv([finding(rule="W1")], {"111": "Widget"})
    link = download_link_html(csv_text, "findings.csv", "Download the detail")
    assert 'download="findings.csv"' in link
    assert "Download the detail" in link
    payload = link.split("base64,")[1].split('"')[0]
    assert base64.b64decode(payload).decode("utf-8") == csv_text


# --- Fix wave E: the file total is the file's rows, not its pairs ----------


def test_total_rows_is_the_file_row_count_not_the_pair_count():
    """Two legal lines for one sku+account are two rows but one pair."""
    comps = {("111", "acct_one"): comp()}
    model = build_model([], comps, {("111", "acct_one")}, {}, total_rows=3)
    assert model.total_rows == 3


def test_render_html_names_the_third_term_when_rows_were_combined():
    comps = {("111", "acct_one"): comp()}
    model = build_model([], comps, {("111", "acct_one")}, {}, total_rows=3)
    html = render_html(model)
    assert "1 written + 0 blocked + 2 combined" in html
    assert "= 3 price rows in the file." in html


def test_render_html_omits_the_third_term_when_nothing_was_combined():
    model = build_model([finding(rule="B1", severity="blocking", sku="9")],
                        {("1", "acct_one"): comp(), ("9", "acct_one"): comp()},
                        {("1", "acct_one")}, {}, total_rows=2)
    assert "combined" not in render_html(model)
