"""Presentation data for the reconciler UI, derived from the domain
vocabulary — status badges and the human-override dropdown."""
from __future__ import annotations

from .domain import (LINK_ERROR, MATCH, NO_BLOGGER,
                     NO_BLOGGER_NOT_IN_PERIMETER, NO_POST,
                     NO_POST_IN_PERIMETER, REVIEW, S_TEXT)

# status → (css class, badge label)
STATUS_BADGES = {
    MATCH: ("match", "MATCH"),
    NO_POST: ("nopost", f"{S_TEXT[NO_POST]} NO_POST"),
    NO_BLOGGER: ("noblogger", f"{S_TEXT[NO_BLOGGER]} NO_BLOGGER"),
    LINK_ERROR: ("linkerror", f"{S_TEXT[LINK_ERROR]} LINK_ERROR"),
    REVIEW: ("review", f"{S_TEXT[REVIEW]} REVIEW"),
    NO_POST_IN_PERIMETER: ("periin", "Perimeter内 无帖子"),
    NO_BLOGGER_NOT_IN_PERIMETER: ("periout", "不在Perimeter"),
}
