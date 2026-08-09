# Copyright (c) serrebidev and contributors
# This file is part of blindDL.
# SPDX-License-Identifier: MIT

"""The order a search asks its sites for, shared by every backend.

This is not the same thing as the Sort by control in the results list. Sort
by rearranges rows that have already arrived; the order here goes out with
the query and decides *which* rows arrive at all. The difference matters
because every site is asked for a page of results and blindDL keeps only the
best two dozen of them: sorting a page of the most-downloaded items by date
finds the newest of the popular ones, never the newest.

Not every site can answer all three. A site that cannot is asked for its own
best match and said so, rather than being quietly reordered afterwards into
something that looks like an answer and is not.
"""

ORDER_RELEVANCE = "relevance"
ORDER_RECENT = "recent"
ORDER_POPULAR = "popular"
ORDERS = (ORDER_RELEVANCE, ORDER_RECENT, ORDER_POPULAR)

# What the choice reads as. "Best match" rather than "Relevance", so it is
# not mistaken for the results list's own Relevance sort when heard.
ORDER_LABELS = {
    ORDER_RELEVANCE: "Best match",
    ORDER_RECENT: "Most recent",
    ORDER_POPULAR: "Most popular",
}
ORDER_LABEL_LIST = [ORDER_LABELS[order] for order in ORDERS]


def normalize(order):
    """Return a known order, falling back to best match.

    Backends take the order straight from saved config and from callers that
    predate it, so an unknown or missing value has to mean "as before".
    """
    return order if order in ORDERS else ORDER_RELEVANCE


def label(order):
    return ORDER_LABELS.get(normalize(order), ORDER_LABELS[ORDER_RELEVANCE])


def supported(support, source, order):
    """Whether one source can answer *order* itself.

    *support* is a backend's {source: frozenset of orders} map. Best match is
    always supported: it is what a site does when asked for nothing.
    """
    order = normalize(order)
    if order == ORDER_RELEVANCE:
        return True
    return order in support.get(source, frozenset())


def unsupported_sources(support, sources, order):
    """The sources that will answer by best match despite being asked otherwise."""
    return [source for source in sources or ()
            if not supported(support, source, order)]


def rank_key(order, score, position, *, popularity=None, published=None):
    """The sort key one result gets inside a backend's own ranking.

    Best match puts the closest match first, which is what the score is for.
    The other two orders keep whatever order the site itself replied in --
    that reply *is* the answer to "newest" or "most popular", and rescoring
    it would throw the answer away. Sites that cannot sort but do publish a
    figure to sort on pass it as *popularity* or *published* so their rows
    can still be put in the asked-for order here.
    """
    order = normalize(order)
    if order == ORDER_POPULAR and popularity is not None:
        return (-float(popularity), position)
    if order == ORDER_RECENT and published is not None:
        return (-float(published), position)
    if order == ORDER_RELEVANCE:
        return (-float(score), position)
    return (0.0, position)
