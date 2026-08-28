import pytest
from vtex_fixed_price_uploader.money import money, same


def test_money_rounds_float_artifacts():
    assert money(4.790000000000001) == 4.79


def test_money_accepts_strings():
    assert money("12.99") == 12.99


def test_money_passes_none_through():
    assert money(None) is None


def test_same_is_true_within_tolerance():
    assert same(9.99, 9.9900001) is True


def test_same_is_false_across_cents():
    assert same(9.99, 9.98) is False


def test_same_is_false_when_either_is_none():
    assert same(None, 9.99) is False
    assert same(9.99, None) is False
    assert same(None, None) is False
