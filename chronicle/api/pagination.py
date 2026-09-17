"""Page-number pagination for the drafts board, submissions list, and
import list: the three lists spec's UI section can grow past a screenful.
Fixed page size, in-memory slicing after the full list is already read
(the same one each page already fetched before this existed), since none
of these three lists is large enough yet to need a database-level offset.
"""

from __future__ import annotations

from dataclasses import dataclass

PAGE_SIZE = 50


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    page: int
    page_size: int
    total: int

    @property
    def total_pages(self) -> int:
        if self.total == 0:
            return 1
        return -(-self.total // self.page_size)  # ceiling division

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def paginate[T](items: list[T], page: int, page_size: int = PAGE_SIZE) -> Page[T]:
    """`page` is 1-indexed and clamped into range; a page past the end
    returns the last page's items rather than an empty list or an error, so
    a stale bookmark or a shrinking list never 404s.
    """
    total = len(items)
    total_pages = 1 if total == 0 else -(-total // page_size)
    clamped = max(1, min(page, total_pages))
    start = (clamped - 1) * page_size
    page_items = items[start : start + page_size]
    return Page(items=page_items, page=clamped, page_size=page_size, total=total)
