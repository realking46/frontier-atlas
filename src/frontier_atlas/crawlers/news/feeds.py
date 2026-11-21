"""RSS 2.0 / Atom parsing with no assumptions about element order.

Atom entries often list <updated> before <published>; we always take
<published>/<pubDate>/<dc:date> and never fall back to <updated>, because an
edit is not a publication.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass(slots=True)
class FeedItem:
    link: str
    title: str
    published_raw: str | None
    summary_html: str | None = None
    author: str | None = None
    content_html: str | None = None     # <content:encoded> / Atom <content>: the body the publisher syndicates


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


PUBLISHED_TAGS = ("pubDate", "published", "date", "issued")   # date/issued = Dublin Core


def parse_feed(xml_text: str) -> list[FeedItem]:
    root = ET.fromstring(xml_text.lstrip("﻿").strip())
    items: list[FeedItem] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        link = title = published = summary = author = content = None
        for ch in node:
            t = _local(ch.tag)
            if t == "link":
                href = ch.attrib.get("href")
                rel = ch.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = link or href
                elif ch.text and ch.text.strip():
                    link = link or ch.text.strip()
            elif t == "guid" and not link and (ch.text or "").startswith("http"):
                link = ch.text.strip()
            elif t == "title":
                title = " ".join((ch.text or "").split())
            elif t in PUBLISHED_TAGS and not published:
                published = (ch.text or "").strip() or None
            elif t in ("description", "summary", "encoded", "content"):
                if not summary:
                    summary = ch.text
                # <content:encoded> / Atom <content>; NOT Media RSS <media:content> (an empty enclosure)
                if t in ("encoded", "content") and "mrss" not in ch.tag and (ch.text or "").strip() and not content:
                    content = ch.text
            elif t in ("creator", "author"):
                name = ch.find("{*}name")
                author = author or ((name.text if name is not None else ch.text) or "").strip() or None
        if link and title:
            items.append(FeedItem(link.strip(), title, published, summary, author, content))
    return items
