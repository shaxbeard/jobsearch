#!/usr/bin/env python3
"""
Shared programming-language detection and ranking logic.

This is the single source of truth for *which* languages are looked for and
*how* they're detected/counted/ranked in a job posting. Both pipelines import
from here so their results are directly comparable:

  - language_trends.py    -- ranks languages from the ATS-API crawl
                             (tracked companies in ats_companies.json).
  - google_language_trends.py -- ranks languages from postings discovered via a
                             general Google (Serper) "engineer | developer"
                             search per city.

Because both use this exact module, any difference in their language rankings
for a city reflects *coverage* (which postings each pipeline finds), not a
difference in detection methodology.

A "posting" here is any dict with at least "title" and "description" string
keys; only those two fields are scanned.
"""

from __future__ import annotations

import re

# Broad candidate list of languages to detect and rank -- not a filter.
# Growing this list costs nothing (it never excludes postings), so it's
# deliberately generous to avoid missing a language that turns out to be
# in a city's top 5/10.
#
# A few single-token names (Go, R, C) are common English words too, so
# instead of matching them bare (which would drown in false positives like
# "we go the extra mile" or "Series C"), we match on safer, more specific
# phrases. That trades some recall for much better precision -- consistent
# with "good enough for market signal, not 100% recall".
LANGUAGE_KEYWORDS: dict[str, list[str]] = {
    "JavaScript/TypeScript": ["javascript", "typescript"],
    "Python": ["python"],
    "Java": ["java"],
    "C#": ["c#"],
    "C++": ["c++"],
    "Go": ["golang", "go programming", "go developer", "go engineer"],
    "Rust": ["rust"],
    "Ruby": ["ruby"],
    "PHP": ["php"],
    "Swift": ["swift"],
    "Kotlin": ["kotlin"],
    "Scala": ["scala"],
    "R": ["rstudio", "r programming", "r language", "tidyverse"],
    "Perl": ["perl"],
    "Objective-C": ["objective-c", "objective c"],
    "Dart": ["dart"],
    "Elixir": ["elixir"],
    "Haskell": ["haskell"],
    "Lua": ["lua"],
    "Shell/Bash": ["bash", "shell scripting", "shell script"],
    "MATLAB": ["matlab"],
    "Groovy": ["groovy"],
    "SQL": ["sql"],
}

# Frameworks, cloud platforms, and other tools to detect alongside languages.
# Same generous, phrase-based, precision-over-recall approach as LANGUAGE_KEYWORDS.
#
# React/Angular/Vue/Node are unambiguous enough as bare words in a job-ad
# context to include directly (verified against real postings: bare "react"
# matched 299/1026 postings vs. only 15 for "react.js"/"reactjs", with just
# ~2 verb-usage false positives like "react to the industry"). "Node" alone
# is included too since most bare mentions turn out to mean Node.js (e.g.
# "node and express", "node or python") rather than infra "node".
TOOL_KEYWORDS: dict[str, list[str]] = {
    "React": ["react"],
    "Angular": ["angular"],
    "Vue.js": ["vue"],
    "Node.js": ["node"],
    "Next.js": ["next.js", "nextjs"],
    "Django": ["django"],
    "Flask": ["flask"],
    "FastAPI": ["fastapi"],
    "Spring": ["spring boot", "spring framework"],
    ".NET": [".net core", ".net framework", "asp.net"],
    "Ruby on Rails": ["ruby on rails"],
    "AWS": ["aws", "amazon web services"],
    "Azure": ["azure"],
    "Google Cloud": ["gcp", "google cloud"],
    "Docker": ["docker"],
    "Kubernetes": ["kubernetes", "k8s"],
    "Terraform": ["terraform"],
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "MongoDB": ["mongodb"],
    "Redis": ["redis"],
    "GraphQL": ["graphql"],
    "Kafka": ["kafka"],
    "Elasticsearch": ["elasticsearch"],
    "Jenkins": ["jenkins"],
    "Git": ["git"],
    "Apache Spark": ["apache spark", "pyspark"],
    "Hadoop": ["hadoop"],
    "TensorFlow": ["tensorflow"],
    "PyTorch": ["pytorch"],
}

# Phrases containing regex-special or too-short-for-\b characters get a
# plain substring check instead of a word-boundary regex.
SUBSTRING_ONLY_PHRASES = {
    "c#",
    "c++",
    "next.js",
    ".net core",
    ".net framework",
    "asp.net",
}


def phrase_in_text(phrase: str, lowercase_haystack: str) -> bool:
    if phrase in SUBSTRING_ONLY_PHRASES:
        return phrase in lowercase_haystack
    return re.search(rf"\b{re.escape(phrase)}\b", lowercase_haystack) is not None


def languages_in_posting(posting: dict) -> list[str]:
    """Return the list of candidate languages detected in one posting."""
    haystack = f"{posting['title']} {posting['description']}".lower()
    return [
        lang
        for lang, phrases in LANGUAGE_KEYWORDS.items()
        if any(phrase_in_text(phrase, haystack) for phrase in phrases)
    ]


def count_languages(postings: list[dict]) -> dict[str, int]:
    counts = {lang: 0 for lang in LANGUAGE_KEYWORDS}
    for posting in postings:
        for lang in languages_in_posting(posting):
            counts[lang] += 1
    return counts


def count_from_matched_languages(matched_lists: list[list[str]]) -> dict[str, int]:
    """Tally language counts from already-detected per-posting language lists.

    Use this to (re)compute a city's aggregate counts over postings whose
    languages were detected on a previous run and stored (e.g. the
    `matched_languages` column in the database), so accumulated data can be
    re-ranked without re-scanning every description.
    """
    counts = {lang: 0 for lang in LANGUAGE_KEYWORDS}
    for langs in matched_lists:
        for lang in langs:
            if lang in counts:
                counts[lang] += 1
    return counts


def tools_in_posting(posting: dict) -> list[str]:
    """Return the list of candidate tools/frameworks detected in one posting."""
    haystack = f"{posting['title']} {posting['description']}".lower()
    return [
        tool
        for tool, phrases in TOOL_KEYWORDS.items()
        if any(phrase_in_text(phrase, haystack) for phrase in phrases)
    ]


def count_tools(postings: list[dict]) -> dict[str, int]:
    counts = {tool: 0 for tool in TOOL_KEYWORDS}
    for posting in postings:
        for tool in tools_in_posting(posting):
            counts[tool] += 1
    return counts


def count_from_matched_tools(matched_lists: list[list[str]]) -> dict[str, int]:
    """Tally tool counts from already-detected per-posting tool lists.

    Same idea as count_from_matched_languages: use this to (re)rank a city's
    tools from the stored `matched_tools` column instead of re-scanning every
    description's text on every read.
    """
    counts = {tool: 0 for tool in TOOL_KEYWORDS}
    for tools in matched_lists:
        for tool in tools:
            if tool in counts:
                counts[tool] += 1
    return counts


def rank_tools(counts: dict[str, int], total_matched: int) -> list[dict]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = []
    for i, (tool, count) in enumerate(ordered):
        percent = round(100 * count / total_matched, 1) if total_matched else 0.0
        ranked.append({"rank": i + 1, "tool": tool, "count": count, "percent": percent})
    return ranked


def rank_languages(counts: dict[str, int], total_matched: int) -> list[dict]:
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = []
    for i, (lang, count) in enumerate(ordered):
        percent = round(100 * count / total_matched, 1) if total_matched else 0.0
        ranked.append({"rank": i + 1, "language": lang, "count": count, "percent": percent})
    return ranked


def print_city_report(city: str, total_matched: int, ranked: list[dict], top: int) -> None:
    print(f"\n=== {city.title()} ({total_matched} matched posting(s)) ===")
    if total_matched == 0:
        print("  (no postings matched this city)")
        return
    shown = [row for row in ranked if row["count"] > 0][:top]
    if not shown:
        print("  (no candidate languages detected in matched postings)")
        return
    for row in shown:
        print(f"  {row['rank']:>2}. {row['language']:<12} {row['count']:>4}  ({row['percent']}%)")
