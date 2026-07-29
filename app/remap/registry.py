"""Per-product column registries for the header mapper.

Each entry: (canonical header text, normalized key, required, description
shown to the model AND to the human auditor). The canonical text must
round-trip through header_key() to the key — tests enforce this.

"plog"/"dmr" belong to the reconciler; "eff" to the efficiency product.
"""
from __future__ import annotations

FIELDS: dict[str, list[tuple[str, str, bool, str]]] = {
    "plog": [
        ("NAME", "name", True, "KOL / blogger display name"),
        ("POST LINK", "postlink", True,
         "URL of the Xiaohongshu post (often an xhslink.com short link)"),
        ("CAMPAIGN", "campaign", False, "campaign / wave the row belongs to"),
        ("NO", "no", False, "row number within the campaign"),
        ("POST DATE", "postdate", False, "date the post went live"),
        ("LIKE", "like", False, "like count"),
        ("COLLECTION", "collection", False, "collect/save count"),
        ("COMMENT", "comment", False, "comment count"),
        ("IMPRESSION", "impression", False, "impression / view count"),
        ("TTL ENGAGEMENT", "ttlengagement", False,
         "total engagement (like + collection + comment)"),
    ],
    "dmr": [
        ("Blogger", "blogger", True, "blogger display name"),
        ("PostID", "postid", True,
         "Xiaohongshu note id — 24-char hex string"),
        ("Username", "username", False,
         "platform author/user id (join key for blogger presence)"),
        ("PostDate", "postdate", False, "crawl-recorded post date"),
        ("Likes_Retweet", "likes_retweet", False, "likes at first crawl"),
        ("Share_Favorites", "share_favorites", False, "shares/favorites"),
        ("Engagement", "engagement", False, "total engagement at crawl"),
        ("WEIGHTED ENG.", "weightedeng.", False,
         "weighted engagement score at crawl"),
        ("Comments", "comments", False, "comment count at crawl"),
        ("Link", "link", False,
         "post URL (hyperlink cell often embeds the note id)"),
    ],
    "eff": [
        ("NO", "no", True, "row number"),
        ("CAMPAIGN", "campaign", True, "campaign / wave name"),
        ("TYPE", "type", True,
         "cooperation type — 报备 (declared/paid) vs 软植 (soft placement)"),
        ("LEVEL", "level", True, "tier label — 头部/腰部/尾部/底部/KOC"),
        ("NAME", "name", True, "KOL / blogger display name"),
        ("FAN BASE（K)", "fanbase(k)", True, "follower count in thousands"),
        ("POST DATE", "postdate", True, "date the post went live"),
        ("POST LINK", "postlink", True, "URL of the post"),
        ("IMPRESSION", "impression", True, "impression / view count"),
        ("LIKE", "like", True, "like count"),
        ("COLLECTION", "collection", True, "collect/save count"),
        ("COMMENT", "comment", True, "comment count"),
        ("TTL ENGAGEMENT", "ttlengagement", True,
         "total engagement (like + collection + comment)"),
        ("PRICE", "price", True, "collaboration price in CNY"),
    ],
}

KIND_LABELS = {"plog": "KOL tracker", "dmr": "DMR export",
               "eff": "KOL efficiency workbook"}
