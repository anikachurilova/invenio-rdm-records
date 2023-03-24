# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 TU Wien.
#
# Invenio RDM Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.

"""Permission factories for Invenio-Stats.

In contrast to the very liberal defaults provided by Invenio-Stats, these permission
factories deny access unless otherwise specified.
"""

from flask import current_app
from invenio_stats.proxies import current_stats


def _get_query(query_name):
    """Build the statistics query from configuration."""
    query_config = current_stats.queries[query_name]
    return query_config.cls(name=query_config.name, **query_config.params)


def get_record_stats(recid, parent_recid):
    """Fetch the statistics for the given record."""
    try:
        views = _get_query("record-view").run(recid=recid)
        views_all = _get_query("record-view-all-versions").run(
            parent_recid=parent_recid
        )
    except Exception as e:
        # e.g. opensearchpy.exceptions.NotFoundError
        # when the aggregation search index hasn't been created yet
        current_app.logger.warning(e)

        fallback_result = {
            "views": 0,
            "unique_views": 0,
        }
        views = views_all = downloads = downloads_all = fallback_result

    try:
        downloads = _get_query("record-download").run(recid=recid)
        downloads_all = _get_query("record-download-all-versions").run(
            parent_recid=parent_recid
        )
    except Exception as e:
        # same as above, but for failure in the download statistics
        # because they are a separate index that can fail independently
        current_app.logger.warning(e)

        fallback_result = {
            "downloads": 0,
            "unique_downloads": 0,
            "data_volume": 0,
        }
        downloads = downloads_all = fallback_result

    stats = {
        "this_version": {
            **views,
            **downloads,
        },
        "all_versions": {
            **views_all,
            **downloads_all,
        },
    }

    return stats
