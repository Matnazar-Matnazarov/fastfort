"""Tests for `ListQuery`, the boundary where untrusted input becomes a query.

Most of these are security tests in disguise: everything downstream trusts what
`from_params` returns, so anything it lets through reaches the database.
"""

from __future__ import annotations

import pytest

from fastfort.spec import Filter, FilterOperator, ListQuery, Page, SortSpec
from fastfort.spec.query import MAX_FILTERS, MAX_IN_VALUES, MAX_SEARCH_LENGTH, MAX_SORT_TERMS

SORTABLE = ("id", "name", "created_at", "category__name")
FILTERABLE = ("is_active", "category", "created_at", "category__name")


def build(params: dict[str, str], **kwargs: object) -> ListQuery:
    return ListQuery.from_params(
        params,
        sortable_fields=SORTABLE,
        filterable_fields=FILTERABLE,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_ordering_accepts_known_fields_in_order() -> None:
    query = build({"o": "-created_at,name"})
    assert query.ordering == (SortSpec("created_at", descending=True), SortSpec("name"))


def test_unknown_sort_field_is_dropped_not_executed() -> None:
    """An unknown key must never reach ORDER BY -- that is the injection path."""
    query = build({"o": "name,password); DROP TABLE users;--"})
    assert query.ordering == (SortSpec("name"),)
    assert query.rejected


def test_ordering_falls_back_to_the_default() -> None:
    default = (SortSpec("id", descending=True),)
    assert build({"o": "nonsense"}, default_ordering=default).ordering == default
    assert build({}, default_ordering=default).ordering == default


def test_ordering_is_capped_and_deduplicated() -> None:
    query = build({"o": "id,name,created_at,category__name"})
    assert len(query.ordering) == MAX_SORT_TERMS

    assert build({"o": "name,-name"}).ordering == (SortSpec("name"),)


@pytest.mark.parametrize("token", ["", "   ", "-", "  -  ", ","])
def test_degenerate_sort_tokens_are_ignored(token: str) -> None:
    assert build({"o": token}).ordering == ()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_bare_field_defaults_to_exact() -> None:
    assert build({"is_active": "1"}).filters == (Filter("is_active", FilterOperator.EXACT, "1"),)


def test_operator_suffix_is_parsed() -> None:
    assert build({"created_at__gte": "2026-01-01"}).filters == (
        Filter("created_at", FilterOperator.GTE, "2026-01-01"),
    )


def test_relation_paths_are_not_mistaken_for_operators() -> None:
    """`category__name` is a field, not `category` with an operator `name`."""
    assert build({"category__name": "phones"}).filters == (
        Filter("category__name", FilterOperator.EXACT, "phones"),
    )


def test_unknown_filter_field_is_rejected() -> None:
    query = build({"is_superuser": "1"})
    assert query.filters == ()
    assert "is_superuser" in query.rejected


def test_unknown_operator_is_rejected() -> None:
    query = build({"created_at__regex": ".*"})
    assert query.filters == ()
    assert "created_at__regex" in query.rejected


def test_multi_value_operators_split_on_commas() -> None:
    query = build({"category__in": "1,2,3"})
    assert query.filters == (Filter("category", FilterOperator.IN, ("1", "2", "3")),)


def test_multi_value_operator_with_no_values_is_rejected() -> None:
    assert build({"category__in": ""}).filters == ()


def test_in_values_are_capped() -> None:
    query = build({"category__in": ",".join(str(i) for i in range(500))})
    assert isinstance(query.filters[0].value, tuple)
    assert len(query.filters[0].value) == MAX_IN_VALUES


def test_filter_count_is_capped() -> None:
    params = {"category__in": "1"} | {f"unknown{i}": "1" for i in range(50)}
    query = build(params)
    assert len(query.filters) <= MAX_FILTERS


def test_reserved_params_are_never_filters() -> None:
    query = build({"q": "x", "o": "name", "p": "2", "ps": "10"})
    assert query.filters == ()
    assert query.rejected == ()


# ---------------------------------------------------------------------------
# Pagination and search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("7", 7), ("0", 1), ("-3", 1), ("abc", 1), ("", 1), ("1e9", 1)],
)
def test_page_is_coerced_and_floored(raw: str, expected: int) -> None:
    assert build({"p": raw}).page == expected


def test_page_size_is_clamped_to_the_maximum() -> None:
    """An unbounded page size is a denial-of-service vector."""
    assert build({"ps": "1000000"}, max_page_size=200).page_size == 200
    assert build({"ps": "0"}, page_size=25).page_size == 1
    assert build({"ps": "junk"}, page_size=25).page_size == 25


def test_search_is_trimmed_and_truncated() -> None:
    assert build({"q": "  phone  "}).search == "phone"
    assert len(build({"q": "x" * 5000}).search) == MAX_SEARCH_LENGTH


def test_search_is_ignored_when_the_model_is_not_searchable() -> None:
    assert build({"q": "phone"}, searchable=False).search == ""


def test_offset_follows_page_and_size() -> None:
    query = build({"p": "3", "ps": "20"})
    assert (query.offset, query.limit) == (40, 20)


# ---------------------------------------------------------------------------
# Immutability and serialisation
# ---------------------------------------------------------------------------


def test_query_is_immutable() -> None:
    query = build({"p": "2"})
    with pytest.raises(AttributeError):
        query.page = 5  # type: ignore[misc]


def test_rejected_does_not_affect_equality() -> None:
    """Two queries that will produce the same SQL compare equal."""
    assert build({"o": "name"}) == build({"o": "name,bogus"})


def test_to_dict_is_json_safe() -> None:
    import json

    payload = build({"q": "x", "o": "-name", "category__in": "1,2"}).to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_page_counters() -> None:
    page: Page[int] = Page(items=(1, 2, 3), total=25, page=2, page_size=10)
    assert page.pages == 3
    assert page.has_previous
    assert page.has_next
    assert (page.start_index, page.end_index) == (11, 13)


def test_empty_page_has_no_indices() -> None:
    page: Page[int] = Page(items=(), total=0, page=1, page_size=10)
    assert (page.pages, page.start_index, page.end_index) == (0, 0, 0)
    assert not page.has_next
    assert not page.has_previous


def test_last_page_has_no_next() -> None:
    page: Page[int] = Page(items=(1,), total=21, page=3, page_size=10)
    assert page.pages == 3
    assert not page.has_next


def test_sort_spec_round_trips_through_its_token() -> None:
    for token in ("name", "-name"):
        assert SortSpec.parse(token).as_token() == token  # type: ignore[union-attr]


def test_page_with_no_size_reports_no_pages() -> None:
    assert Page(items=(), total=5, page=1, page_size=0).pages == 0
