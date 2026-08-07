"""Similarity search, at the boundary where a query string becomes a query.

Nearest-neighbour search is an *ordering*, not a filter -- every row has a
distance and the question is which are smallest -- so it lives in its own field
on `ListQuery` rather than among the filters, for the same reason the spatial
conditions do: it compiles to SQL only one of the three databases has.

What is tested here is everything up to the SQL. That the ordering actually
ranks correctly is tested against a live pgvector in `tests/ui/test_admin_vector.py`,
because no amount of parsing proves that cosine distance was applied rather
than L2.
"""

from __future__ import annotations

import pytest

from fastfort.spec import ListQuery, VectorMetric

#: Three dimensions, so a test can be read rather than trusted.
VECTORS = {"embedding": 3, "loose": None}


def build(params: dict[str, str], **kwargs: object) -> ListQuery:
    return ListQuery.from_params(
        params,
        vector_fields=VECTORS,
        filterable_fields=("title",),
        sortable_fields=("title",),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["[1, 0, 0]", "1,0,0", "  [1.0,0.0,0.0]  "])
def test_a_query_vector_is_read_both_ways_people_write_it(text: str) -> None:
    """Bracketed is what pgvector itself prints, so a value read out of the
    database and pasted back has to parse. Bare numbers are what somebody
    types."""
    query = build({"embedding__near": text})

    assert query.vector is not None
    assert query.vector.vector == "[1.0,0.0,0.0]"


def test_the_vector_is_re_rendered_rather_than_passed_through() -> None:
    """It ends up as an operand of a SQL operator. The one thing it must not be
    is the text a request chose -- so what reaches the query builder is a string
    this package built out of numbers it parsed."""
    query = build({"embedding__near": "[1e0, 2, 3]"})

    assert query.vector is not None
    assert query.vector.vector == "[1.0,2.0,3.0]"


def test_the_metric_is_part_of_the_query() -> None:
    """The wrong one is not an error, it is a silently worse ranking -- cosine
    and L2 agree on normalised vectors and disagree on everything else."""
    query = build({"embedding__near": "[1,0,0]", "embedding__metric": "l2"})

    assert query.vector is not None
    assert query.vector.metric is VectorMetric.L2


def test_cosine_is_the_default() -> None:
    """What almost every text-embedding model is trained for."""
    query = build({"embedding__near": "[1,0,0]"})

    assert query.vector is not None
    assert query.vector.metric is VectorMetric.COSINE


def test_a_column_with_no_declared_width_takes_any() -> None:
    """pgvector allows `Vector()` with no dimension, and a column that never
    said cannot have its width checked."""
    query = build({"loose__near": "[1,2,3,4,5]"})

    assert query.vector is not None
    assert query.vector.field == "loose"


def test_an_ordinary_filter_survives_alongside_a_search() -> None:
    """`field__near` and `field__gte` are the same shape, so the vector keys
    have to be taken out before the ordinary parser sees them."""
    query = build({"embedding__near": "[1,0,0]", "title": "shop"})

    assert query.vector is not None
    assert len(query.filters) == 1
    assert query.rejected == ()


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_a_column_that_is_not_a_vector_is_never_searched() -> None:
    """`vector_fields` is the allow-list. Without it any column name plus the
    suffix would reach a distance operator."""
    assert build({"title__near": "[1,0,0]"}).vector is None


def test_a_vector_of_the_wrong_width_is_refused_here() -> None:
    """Not left to the database. "expected 3 dimensions" arrives from the driver
    as a failed transaction; refused here it is a rejected parameter and the
    page still renders."""
    query = build({"embedding__near": "[1,0]"})

    assert query.vector is None
    assert "embedding__near" in query.rejected


@pytest.mark.parametrize("text", ["", "[]", "not a vector", "[1, two, 3]"])
def test_something_that_is_not_a_vector_is_not_one(text: str) -> None:
    assert build({"embedding__near": text}).vector is None


def test_an_unknown_metric_is_refused_rather_than_silently_replaced() -> None:
    """Falling back to cosine would rank by a measure nobody asked for and say
    nothing about it."""
    query = build({"embedding__near": "[1,0,0]", "embedding__metric": "euclidean-ish"})

    assert query.vector is None
    assert "embedding__metric" in query.rejected


def test_a_vector_longer_than_any_model_produces_is_refused() -> None:
    """A bound on the parser, not on the models: the largest embedding in use is
    3 072 numbers, and a query string is not a place to accept an unbounded
    list."""
    assert build({"embedding__near": "[" + ",".join(["1"] * 5000) + "]"}).vector is None


def test_the_neighbour_count_is_clamped() -> None:
    """A nearest-neighbour index answers "the closest 100" in milliseconds and
    "the closest 100 000" no faster than a sequential scan."""
    query = build({"embedding__near": "[1,0,0]", "embedding__k": "999999"})

    assert query.vector is not None
    assert query.vector.limit == 1_000


def test_only_one_search_per_request() -> None:
    """Two would be two orderings, and "nearest to A, then nearest to B" is not
    a question with an answer."""
    query = build({"embedding__near": "[1,0,0]", "loose__near": "[1,2,3]"})

    assert query.vector is not None
    # Whichever it took, it took exactly one.
    assert isinstance(query.vector.field, str)
