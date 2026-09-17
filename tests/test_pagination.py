"""Pagination boundaries: page size, clamping, next/previous links."""

from __future__ import annotations

from chronicle.api.pagination import Page, paginate


def test_paginate_first_page_has_no_previous() -> None:
    pg = paginate(list(range(120)), page=1)
    assert pg.items == list(range(50))
    assert pg.page == 1
    assert pg.total == 120
    assert pg.total_pages == 3
    assert not pg.has_previous
    assert pg.has_next


def test_paginate_last_page_has_no_next_and_may_be_short() -> None:
    pg = paginate(list(range(120)), page=3)
    assert pg.items == list(range(100, 120))
    assert len(pg.items) == 20
    assert pg.has_previous
    assert not pg.has_next


def test_paginate_exact_multiple_of_page_size_has_no_trailing_empty_page() -> None:
    pg = paginate(list(range(100)), page=2)
    assert pg.total_pages == 2
    assert len(pg.items) == 50
    assert not pg.has_next


def test_paginate_page_past_the_end_clamps_to_the_last_page() -> None:
    pg = paginate(list(range(10)), page=99)
    assert pg.page == 1
    assert pg.items == list(range(10))


def test_paginate_empty_list_is_page_one_of_one() -> None:
    pg: Page[int] = paginate([], page=1)
    assert pg.page == 1
    assert pg.total_pages == 1
    assert pg.items == []
    assert not pg.has_previous
    assert not pg.has_next


def test_paginate_page_zero_clamps_to_page_one() -> None:
    pg = paginate(list(range(10)), page=0)
    assert pg.page == 1
