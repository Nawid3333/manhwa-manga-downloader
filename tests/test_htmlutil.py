"""Tests for src/htmlutil.py: the lxml/XPath helpers every driver parses through."""

from __future__ import annotations

from src.htmlutil import all_of, attr, first, has_class, parse_html, spaced_text, stripped_text


def test_parse_html_empty_returns_none():
    assert parse_html("") is None
    assert parse_html("   \n\t") is None


def test_parse_html_parses_valid_markup():
    doc = parse_html("<html><body><p id='a'>hi</p></body></html>")
    assert doc is not None
    assert first(doc, "//p[@id='a']") is not None


def test_first_and_all_of_handle_none_doc():
    assert first(None, "//p") is None
    assert all_of(None, "//p") == []


def test_first_returns_first_match():
    doc = parse_html("<div><p>one</p><p>two</p></div>")
    node = first(doc, "//p")
    assert node is not None
    assert node.text == "one"


def test_all_of_returns_every_match():
    doc = parse_html("<div><p>one</p><p>two</p></div>")
    nodes = all_of(doc, "//p")
    assert [n.text for n in nodes] == ["one", "two"]


def test_attr_missing_and_present():
    doc = parse_html("<a href='/x'>link</a>")
    node = first(doc, "//a")
    assert attr(node, "href") == "/x"
    assert attr(node, "missing") is None
    assert attr(None, "href") is None


def test_stripped_text_has_no_separator():
    doc = parse_html("<p>Harry Potter<small>Specials</small></p>")
    node = first(doc, "//p")
    assert stripped_text(node) == "Harry PotterSpecials"


def test_spaced_text_joins_with_space():
    doc = parse_html("<p>Harry Potter<small>Specials</small></p>")
    node = first(doc, "//p")
    assert spaced_text(node) == "Harry Potter Specials"


def test_has_class_matches_whole_token_only():
    doc = parse_html("<div class='seen active'>a</div><div class='unseen'>b</div>")
    matches = doc.xpath(f"//div[{has_class('seen')}]")
    assert len(matches) == 1
    assert matches[0].get("class") == "seen active"
