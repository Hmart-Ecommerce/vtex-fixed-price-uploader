import typing
from datetime import datetime, timezone

from vtex_fixed_price_uploader.compose import compose
from vtex_fixed_price_uploader.config import load_config
from vtex_fixed_price_uploader.parser import Row
from vtex_fixed_price_uploader.rules import (
    SEVERITY, Finding, blocked_pairs, evaluate)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
CFG = load_config({"accounts": {"R1": "acct_one", "R2": "acct_two"},
                   "never_write": ["acct_master"], "trade_policy": "1"})


def dt(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def row(sku="111", code="R1", account="acct_one", promo=7.99, list_price=8.99,
        start="2026-08-28T00:00:00", end="2026-09-18T00:00:00", line=2):
    return Row(sku=sku, code=code, account=account, promo=promo,
               list_price=list_price,
               start=dt(start) if start else None,
               end=dt(end) if end else None,
               promo_type="weekly", line=line)


def payload(base=8.99, fixed=None):
    return {"basePrice": base, "fixedPrices": fixed or []}


def entry(value, start=None, end=None, min_qty=1):
    e = {"value": value, "listPrice": None, "minQuantity": min_qty,
         "tradePolicyId": "1"}
    if start or end:
        e["dateRange"] = {"from": start, "to": end}
    return e


def run(rows, reads=None, comps=None):
    reads = reads or {(r.sku, r.account): (200, payload()) for r in rows}
    if comps is None:
        comps = {}
        for r in rows:
            key = (r.sku, r.account)
            data = reads.get(key, (200, None))[1]
            comps[key] = compose([x for x in rows if (x.sku, x.account) == key],
                                 data, NOW)
    return evaluate(rows, reads, comps, CFG, NOW)


def rules_fired(findings, sku="111", account="acct_one"):
    return {f.rule for f in findings
            if f.sku == sku and f.account == account}


def detail_of(findings, rule):
    return [f.detail for f in findings if f.rule == rule][0]


def test_clean_row_fires_nothing():
    r = row(promo=7.99, list_price=8.99)
    assert rules_fired(run([r])) == set()


# --- Fix 1: severity is the safety contract and must be pinned by id. -------

def test_every_rule_severity_is_pinned_by_id():
    """Severity decides what `blocked_pairs()` excludes from the batch.

    W6 (campaign collision) is a WARNING on purpose. That is a recorded
    decision - spec sections 9 and 16 - not an oversight. Review asked for it
    to be promoted to blocking and the project declined, because section 7
    resolves the collision structurally and W6 exists to make it visible, not
    to prevent it. Do not "correct" it to blocking.

    The comparison is exact equality on purpose. A subset check would let a
    new id land unpinned - which is how B6, the failed read, would have been
    swallowed had it been folded into B4 instead of given its own id.
    """
    assert SEVERITY == {
        "B1": "blocking",
        "B2": "blocking",
        "B3": "blocking",
        "B4": "blocking",
        "B5": "blocking",
        "B6": "blocking",
        "B7": "blocking",
        "W1": "warning",
        "W2": "warning",
        "W3": "warning",
        "W4": "warning",
        "W5": "warning",
        "W6": "warning",
        "I1": "info",
    }


def test_a_blocking_rule_actually_excludes_the_pair_from_the_batch():
    r = row(start="2026-09-18T00:00:00", end="2026-08-28T00:00:00")
    findings = run([r])
    assert "B2" in rules_fired(findings)
    assert blocked_pairs(findings) == {("111", "acct_one")}


def test_a_warning_rule_never_excludes_the_pair_from_the_batch():
    r = row(promo=12.99, list_price=12.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    findings = run([r], reads)
    assert "W1" in rules_fired(findings)
    assert blocked_pairs(findings) == set()


def test_b1_same_pair_twice_with_overlapping_windows():
    rows = [row(promo=2.99), row(promo=3.99, line=3)]
    assert "B1" in rules_fired(run(rows))


def test_b1_does_not_fire_for_non_overlapping_windows():
    rows = [row(end="2026-09-05T00:00:00"),
            row(promo=6.99, start="2026-09-06T00:00:00", line=3)]
    assert "B1" not in rules_fired(run(rows))


def test_b1_does_not_fire_across_different_accounts():
    rows = [row(account="acct_one"), row(code="R2", account="acct_two")]
    assert "B1" not in rules_fired(run(rows))


# --- Fix 4: every collision for the pair, not just the first. --------------

def test_b1_reports_every_collision_for_the_pair():
    rows = [row(promo=2.99, line=2), row(promo=3.99, line=3),
            row(promo=4.99, line=4)]
    pairs = {tuple(sorted((f.detail.split()[1], f.detail.split()[3]
                           .rstrip(":"))))
             for f in run(rows) if f.rule == "B1"}
    assert pairs == {("2", "3"), ("2", "4"), ("3", "4")}


# --- Fix 5: B1 must not depend on row order. -------------------------------

def test_b1_is_order_independent_when_one_row_has_no_end_date():
    open_ended = row(start="2026-12-01T00:00:00", end=None, line=2)
    past = row(promo=3.99, start="2026-01-01T00:00:00",
               end="2026-02-01T00:00:00", line=3)
    assert "B1" in rules_fired(run([open_ended, past]))
    assert "B1" in rules_fired(run([past, open_ended]))


# --- Fix 9: the B1 message must match what the rule actually detects. ------

def test_b1_message_does_not_claim_the_prices_differ():
    rows = [row(promo=2.99), row(promo=2.99, line=3)]
    message = [f.message for f in run(rows) if f.rule == "B1"][0]
    assert "different prices" not in message


def test_b1_detail_names_both_lines_and_both_prices():
    rows = [row(promo=2.99), row(promo=3.99, line=3)]
    assert detail_of(run(rows), "B1") == (
        "Lines 2 and 3: $2.99 and $3.99. Pick one.")


def test_b2_end_before_start():
    r = row(start="2026-09-18T00:00:00", end="2026-08-28T00:00:00")
    assert "B2" in rules_fired(run([r]))


def test_b3_window_entirely_in_the_past():
    r = row(start="2026-01-01T00:00:00", end="2026-02-01T00:00:00")
    assert "B3" in rules_fired(run([r]))


def test_b4_sku_missing_from_account():
    r = row()
    reads = {("111", "acct_one"): (404, None)}
    assert "B4" in rules_fired(run([r], reads))


# --- Fix 10: B4 must name a next step. -------------------------------------

def test_b4_detail_tells_the_operator_what_to_do():
    reads = {("111", "acct_one"): (404, None)}
    detail = detail_of(run([row()], reads), "B4")
    assert "Remove this region from your file" in detail


# --- Fix 8: a failed read blocks the row, under its own id B6. -------------
# B4 and B6 are opposite instructions - take the row out of the file versus
# re-run it unchanged - and the rule id is what reaches the downloadable CSV,
# so one id cannot carry both conditions.

def test_a_failed_read_blocks_the_row():
    for status in (0, 401, 429, 500, 503):
        findings = run([row()], {("111", "acct_one"): (status, None)})
        assert blocked_pairs(findings) == {("111", "acct_one")}, status
        assert "B6" in rules_fired(findings), status


def test_b6_severity_is_blocking():
    findings = run([row()], {("111", "acct_one"): (429, None)})
    assert [f.severity for f in findings if f.rule == "B6"] == ["blocking"]


def test_a_failed_read_tells_the_operator_to_re_run():
    findings = run([row()], {("111", "acct_one"): (429, None)})
    detail = detail_of(findings, "B6")
    assert "Re-run" in detail


def test_a_failed_read_and_a_missing_product_are_distinguishable():
    failed = run([row()], {("111", "acct_one"): (429, None)})
    missing = run([row()], {("111", "acct_one"): (404, None)})
    assert rules_fired(failed) == {"B6"}
    assert rules_fired(missing) == {"B4"}
    b6 = [f for f in failed if f.rule == "B6"][0]
    b4 = [f for f in missing if f.rule == "B4"][0]
    assert b6.message != b4.message
    assert b6.detail != b4.detail
    # The missing product has to come out of the file; the unread row must not
    # - nothing is wrong with it.
    assert "Remove this region from your file" in b4.detail
    assert "Remove" not in b6.detail


def test_a_successful_read_is_not_treated_as_a_failed_read():
    findings = run([row()])
    assert blocked_pairs(findings) == set()
    assert "B6" not in rules_fired(findings)


def test_b7_blocks_skus_the_writer_cannot_accept():
    for sku in ("1234.0", "two words", "path/part", "sku#1"):
        findings = run([row(sku=sku)])
        b7 = [finding for finding in findings if finding.rule == "B7"]

        assert len(b7) == 1, sku
        assert b7[0].severity == "blocking"
        assert b7[0].sku == sku
        assert blocked_pairs(findings) == {(sku, "acct_one")}


def test_b7_explains_how_to_fix_a_spreadsheet_formatted_sku():
    finding = [f for f in run([row(sku="1234.0")]) if f.rule == "B7"][0]

    guidance = finding.message + " " + finding.detail
    assert "1234.0" in guidance
    assert "plain identifier" in guidance
    assert "format" in guidance.lower()
    assert "text" in guidance.lower()


def test_b5_zero_price():
    assert "B5" in rules_fired(run([row(promo=0.0)]))


def test_b5_absurd_price():
    assert "B5" in rules_fired(run([row(promo=1000.0)]))


def test_b5_absurd_list_price():
    assert "B5" in rules_fired(run([row(list_price=5000.0)]))


def test_b5_ignores_a_missing_list_price():
    assert "B5" not in rules_fired(run([row(list_price=None)]))


# --- Fix 11: pin B5's boundaries on both sides. ----------------------------

def test_b5_allows_a_price_at_exactly_the_maximum():
    r = row(promo=999.0, list_price=999.0)
    assert "B5" not in rules_fired(run([r]))


def test_b5_blocks_a_price_just_above_the_maximum():
    assert "B5" in rules_fired(run([row(promo=999.01)]))


def test_b5_allows_a_one_cent_price():
    assert "B5" not in rules_fired(run([row(promo=0.01)]))


def test_w1_promo_above_base():
    r = row(promo=12.99, list_price=12.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    fired = rules_fired(run([r], reads))
    assert "W1" in fired
    # Fix 7: W5 would restate the same fact and must stay silent.
    assert "W5" not in fired


def test_w1_detail_names_both_prices():
    r = row(promo=12.99, list_price=12.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    assert detail_of(run([r], reads), "W1") == (
        "Promotion $12.99 against a shelf price of $8.99.")


def test_w2_promo_equals_base():
    r = row(promo=8.99, list_price=8.99)
    assert "W2" in rules_fired(run([r]))


def test_w1_and_w2_are_mutually_exclusive():
    fired = rules_fired(run([row(promo=8.99, list_price=8.99)]))
    assert "W2" in fired and "W1" not in fired


def test_w3_list_price_differs_from_base():
    r = row(promo=7.99, list_price=10.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    assert "W3" in rules_fired(run([r], reads))


def test_w4_discount_deeper_than_sixty_percent():
    r = row(promo=2.99, list_price=8.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    assert "W4" in rules_fired(run([r], reads))


def test_w4_does_not_fire_at_exactly_sixty_percent_off():
    r = row(promo=4.00, list_price=10.00)
    reads = {("111", "acct_one"): (200, payload(base=10.00))}
    assert "W4" not in rules_fired(run([r], reads))


# --- Fix 2: the 60%-off boundary must be exact, not a raw float. -----------

def test_w4_does_not_fire_at_exactly_sixty_percent_off_of_three_dollars():
    # 3.00 * 0.40 == 1.2000000000000002 in binary floating point, so an
    # unrounded comparison fires at precisely the threshold the spec excludes.
    r = row(promo=1.20, list_price=3.00)
    reads = {("111", "acct_one"): (200, payload(base=3.00))}
    assert "W4" not in rules_fired(run([r], reads))


def test_w4_fires_just_inside_the_deep_discount_floor():
    r = row(promo=1.19, list_price=3.00)
    reads = {("111", "acct_one"): (200, payload(base=3.00))}
    assert "W4" in rules_fired(run([r], reads))


def test_w4_does_not_fire_just_outside_the_deep_discount_floor():
    r = row(promo=1.21, list_price=3.00)
    reads = {("111", "acct_one"): (200, payload(base=3.00))}
    assert "W4" not in rules_fired(run([r], reads))


# --- Fix 10: W4's detail must not read as "$2.99 of discount". -------------

def test_w4_detail_does_not_read_as_a_discount_amount():
    r = row(promo=2.99, list_price=8.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    assert detail_of(run([r], reads), "W4") == (
        "Sets a price of $2.99 against a shelf price of $8.99.")


def test_w5_promo_raises_the_price_versus_what_serves_today():
    r = row(promo=7.99, list_price=8.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99,
                                                fixed=[entry(4.99)]))}
    assert "W5" in rules_fired(run([r], reads))


# --- Fix 7: W5 must not restate W1. ---------------------------------------

def test_w5_is_suppressed_when_w1_already_said_it():
    r = row(promo=12.99, list_price=12.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    fired = rules_fired(run([r], reads))
    assert "W1" in fired
    assert "W5" not in fired


def test_w6_dropping_a_campaign_that_would_have_run_longer():
    live = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-10-03T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[live]))}
    assert "W6" in rules_fired(run([row()], reads))


def test_w6_detail_names_the_price_and_the_end_date():
    live = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-10-03T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[live]))}
    assert detail_of(run([row()], reads), "W6") == (
        "$9.99 was set to run until 2026-10-03.")


def test_w6_does_not_fire_for_an_expired_entry():
    dead = entry(9.99, start="2026-04-01T00:00:00Z", end="2026-05-01T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[dead]))}
    assert "W6" not in rules_fired(run([row()], reads))


def test_w6_does_not_fire_when_the_dropped_entry_ends_inside_the_new_window():
    short = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-09-01T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[short]))}
    assert "W6" not in rules_fired(run([row()], reads))


def test_w6_fires_for_an_open_ended_dropped_entry():
    forever = entry(9.99, start="2026-01-01T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[forever]))}
    assert "W6" in rules_fired(run([row()], reads))


# --- Fix 6: W6 compares against the pair's LATEST csv end. -----------------

def test_w6_does_not_fire_when_a_later_csv_row_covers_the_dropped_entry():
    rows = [row(end="2026-09-18T00:00:00", line=2),
            row(promo=6.99, start="2026-09-19T00:00:00",
                end="2026-10-30T00:00:00", line=3)]
    live = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-10-03T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[live]))}
    assert "W6" not in rules_fired(run(rows, reads))


def test_w6_does_not_fire_when_a_csv_row_is_open_ended():
    # Nothing ends after an unbounded end, so the new entry covers everything.
    rows = [row(end=None)]
    live = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-09-25T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[live]))}
    assert "W6" not in rules_fired(run(rows, reads))


def test_w6_is_reported_once_for_one_dropped_entry_across_two_csv_rows():
    rows = [row(end="2026-09-18T00:00:00", line=2),
            row(promo=6.99, start="2026-09-19T00:00:00",
                end="2026-10-01T00:00:00", line=3)]
    live = entry(9.99, start="2026-08-14T00:00:00Z", end="2026-12-01T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[live]))}
    assert len([f for f in run(rows, reads) if f.rule == "W6"]) == 1


# --- Fix 11: the comps argument is covered. --------------------------------

def test_w6_is_silent_when_no_composition_is_supplied_for_the_pair():
    forever = entry(9.99, start="2026-01-01T00:00:00Z")
    reads = {("111", "acct_one"): (200, payload(base=12.99, fixed=[forever]))}
    assert "W6" not in rules_fired(run([row()], reads, comps={}))


def test_i1_region_present_in_account_but_absent_from_the_sheet():
    r = row(code="R1", account="acct_one")
    reads = {("111", "acct_one"): (200, payload()),
             ("111", "acct_two"): (200, payload())}
    findings = run([r], reads)
    assert any(f.rule == "I1" and f.account == "acct_two" for f in findings)


def test_i1_does_not_fire_when_the_sku_is_absent_from_that_account():
    r = row(code="R1", account="acct_one")
    reads = {("111", "acct_one"): (200, payload()),
             ("111", "acct_two"): (404, None)}
    findings = run([r], reads)
    assert not any(f.rule == "I1" for f in findings)


def test_i1_preserves_each_region_code_when_accounts_are_shared():
    config = load_config({
        "accounts": {"R1": "acct_one", "R2": "acct_one"},
        "never_write": ["acct_master"],
        "trade_policy": "1",
    })
    rows = [row(code="R1", account="acct_one")]
    reads = {("111", "acct_one"): (200, payload())}

    findings = evaluate(rows, reads, {}, config, NOW)

    assert [(finding.rule, finding.code) for finding in findings] == [("I1", "R2")]


# --- Fix 3: suppression is gated on a missing base, not on blocked-ness. ---

def test_a_blocked_row_still_reports_warnings_that_were_computable():
    # B2 blocks the row, but the read succeeded, so the price typo is knowable
    # on run one. Hiding it costs the operator a second round trip.
    r = row(start="2026-09-18T00:00:00", end="2026-08-28T00:00:00",
            promo=1.00, list_price=10.99)
    reads = {("111", "acct_one"): (200, payload(base=8.99))}
    fired = rules_fired(run([r], reads))
    assert "B2" in fired
    assert "W3" in fired
    assert "W4" in fired


def test_warnings_are_suppressed_when_the_read_returned_no_base_price():
    r = row(promo=1.00, list_price=10.99)
    reads = {("111", "acct_one"): (404, None)}
    fired = rules_fired(run([r], reads))
    assert "B4" in fired
    assert not any(rule.startswith("W") for rule in fired)


def test_blocked_pairs_returns_sku_account_tuples():
    findings = run([row(promo=0.0)])
    assert blocked_pairs(findings) == {("111", "acct_one")}


def test_every_finding_carries_an_english_message():
    findings = run([row(promo=12.99)],
                   {("111", "acct_one"): (200, payload(base=8.99))})
    assert all(f.message and f.message[0].isupper() for f in findings)


# --- Fix 11: assert the actual order, not that two runs agree. -------------

def test_findings_are_returned_in_blocking_then_warning_then_info_order():
    rows = [row(sku="111", promo=0.0, list_price=8.99, line=2),
            row(sku="222", promo=12.99, list_price=12.99, line=3)]
    reads = {("111", "acct_one"): (200, payload(base=8.99)),
             ("222", "acct_one"): (200, payload(base=8.99))}
    assert [(f.rule, f.sku) for f in run(rows, reads)] == [
        ("B5", "111"), ("W4", "111"), ("W1", "222"), ("W3", "222")]


# --- Fix 12: the annotations the plan states. ------------------------------

def test_evaluate_is_annotated_as_the_plan_states():
    hints = typing.get_type_hints(evaluate)
    assert hints["return"] == list[Finding]
    assert set(hints) >= {"rows", "reads", "compositions", "config", "now",
                          "return"}


def test_blocked_pairs_is_annotated_as_the_plan_states():
    hints = typing.get_type_hints(blocked_pairs)
    assert hints["return"] == set[tuple[str, str]]
