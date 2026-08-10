"""Job-market language-trend tracking toolkit.

Subpackages:
  - :mod:`jobtrends.sources`  -- job-posting discovery/crawling backends.
  - :mod:`jobtrends.analysis` -- language ranking, trend aggregation, stats.
  - :mod:`jobtrends.web`      -- Flask frontend.

Shared foundations live at the top level: :mod:`jobtrends.language_detect`
(language detection) and :mod:`jobtrends.trends_db` (SQLite persistence).
Runtime filesystem locations are resolved in :mod:`jobtrends.paths`.
"""
