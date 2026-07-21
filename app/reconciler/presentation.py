"""Presentation data for the reconciler UI, derived from the domain
vocabulary — status badges and the human-override dropdown."""
from __future__ import annotations

from .domain import (LINK_ERROR, MATCH, NAME_MISLABEL, NO_BLOGGER,
                     NO_BLOGGER_NOT_IN_PERIMETER, NO_POST,
                     NO_POST_IN_PERIMETER, REVIEW, S_TEXT)
from .export import OVERRIDE_MATCH_BLANK

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

OVERRIDE_CHOICES = ["", OVERRIDE_MATCH_BLANK, S_TEXT[NO_BLOGGER],
                    S_TEXT[NO_POST], S_TEXT[LINK_ERROR],
                    NAME_MISLABEL, S_TEXT[REVIEW],
                    S_TEXT[NO_POST_IN_PERIMETER],
                    S_TEXT[NO_BLOGGER_NOT_IN_PERIMETER]]
