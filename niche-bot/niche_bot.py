#!/usr/bin/env python3
"""Ebook niche research bot.

Give it a broad topic (e.g. "personal finance", "dog training") and it will:
  1. Research the web for profitable ebook sub-niches (demand, competition,
     reader pain points, pricing) using Claude's web search + web fetch tools.
  2. Turn that research into a scored, structured shortlist of niches.
  3. Save a Markdown report and a JSON file you can feed into other tools.

Usage:
  python niche_bot.py "personal finance"
  python niche_bot.py "gardening" --count 10 --audience "retirees" --out reports
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import anthropic

MODEL = "claude-opus-5-5"
BETAS = ["server-side-fallback-2026-07-01"]
MAX_CONTINUATIONS = 8  # cap on pause_turn resumes during research

RESEARCH_SYSTEM = """You are a market researcher for a self-publisher who writes \
non-fiction ebooks (Amazon KDP, Gumroad, own website). Your job is to find \
sub-niches where a new, well-made ebook can realistically sell.

Use web search and web fetch to gather real evidence. Good signals:
- Demand: Amazon Kindle bestseller/category pages and rankings, how many recent \
books exist with many reviews, Google/Pinterest/YouTube interest, active \
subreddits and forums, recurring questions people ask.
- Competition: number and quality of existing titles, how dated they are, \
average ratings, common complaints in 1-3 star reviews (gaps to exploit).
- Monetization: typical ebook price points, whether readers already buy courses, \
templates or tools in this space, evergreen vs. fad.

Prefer specific, narrow niches (a clear reader + a clear problem) over broad \
topics. Record concrete evidence with source URLs. Be honest when evidence is \
thin; do not invent numbers, titles or rankings."""

STRUCTURE_SYSTEM = """You convert ebook market research notes into a scored \
shortlist. Use only facts present in the notes; when a score rests on weak \
evidence, say so in the risks field. Scores are integers from 1 to 10. For \
competition_score, 10 means LOW competition (easy to enter). overall_score \
weighs demand and monetization most, then competition."""

SOURCE = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
    "required": ["title", "url"],
    "additionalProperties": False,
}
STR_LIST = {"type": "array", "items": {"type": "string"}}

NICHE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "summary": {"type": "string", "description": "3-5 sentence overview of the opportunity landscape."},
        "niches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "target_reader": {"type": "string"},
                    "problem_solved": {"type": "string"},
                    "demand_score": {"type": "integer", "description": "1-10"},
                    "competition_score": {"type": "integer", "description": "1-10, 10 = low competition"},
                    "monetization_score": {"type": "integer", "description": "1-10"},
                    "overall_score": {"type": "integer", "description": "1-10"},
                    "evidence": STR_LIST,
                    "existing_competitors": STR_LIST,
                    "content_gaps": STR_LIST,
                    "suggested_titles": STR_LIST,
                    "chapter_ideas": STR_LIST,
                    "keywords": STR_LIST,
                    "price_range": {"type": "string"},
                    "risks": {"type": "string"},
                    "sources": {"type": "array", "items": SOURCE},
                },
                "required": [
                    "name", "target_reader", "problem_solved", "demand_score",
                    "competition_score", "monetization_score", "overall_score",
                    "evidence", "existing_competitors", "content_gaps",
                    "suggested_titles", "chapter_ideas", "keywords",
                    "price_range", "risks", "sources",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["topic", "summary", "niches"],
    "additionalProperties": False,
}


class BotError(RuntimeError):
    pass


def _check_stop(message, phase: str) -> None:
    if message.stop_reason == "refusal":
        category = getattr(message.stop_details, "category", None) if message.stop_details else None
        raise BotError(f"{phase}: the model declined this request (category: {category}).")
    if message.stop_reason == "max_tokens":
        raise BotError(f"{phase}: output hit max_tokens before finishing.")


def research(client: anthropic.Anthropic, topic: str, count: int, audience: str | None,
             effort: str, max_searches: int) -> tuple[str, list[dict]]:
    """Phase 1: open-ended web research. Returns (notes, sources)."""
    ask = (
        f"Research ebook niche opportunities within the topic: {topic!r}.\n"
        + (f"Focus on readers who are: {audience}.\n" if audience else "")
        + f"Identify at least {count + 3} candidate sub-niches, investigate them, "
        f"and finish with detailed notes on the best {count}. For each, cover: "
        "target reader, problem solved, demand evidence, competing titles and "
        "their weaknesses, content gaps, typical price range, keyword ideas, "
        "risks, and the source URLs you relied on."
    )
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max_searches},
    ]
    messages: list[dict] = [{"role": "user", "content": ask}]
    all_blocks: list = []  # content across every resumed turn

    for _ in range(MAX_CONTINUATIONS + 1):
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            betas=BETAS,
            fallbacks="default",
            system=RESEARCH_SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        ) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "server_tool_use":
                        print(f"  · {block.name}…", file=sys.stderr, flush=True)
            message = stream.get_final_message()
        all_blocks.extend(message.content)

        if message.stop_reason == "pause_turn":
            # Server-side tool loop paused; send the turn back so it resumes.
            messages.append({"role": "assistant", "content": message.content})
            continue
        _check_stop(message, "Research")
        break
    else:
        raise BotError("Research did not finish within the continuation limit.")

    notes, sources, seen = [], [], set()
    for block in all_blocks:
        if block.type == "text":
            notes.append(block.text)
        elif block.type == "web_search_tool_result" and isinstance(block.content, list):
            for r in block.content:
                if getattr(r, "url", None) and r.url not in seen:
                    seen.add(r.url)
                    sources.append({"title": r.title, "url": r.url})
    text = "\n".join(notes).strip()
    if not text:
        raise BotError("Research returned no notes.")
    return text, sources


def structure(client: anthropic.Anthropic, topic: str, notes: str, sources: list[dict],
              count: int) -> dict:
    """Phase 2: turn the notes into scored JSON."""
    source_lines = "\n".join(f"- {s['title']}: {s['url']}" for s in sources[:80])
    prompt = (
        f"Topic: {topic}\nReturn the top {count} niches, best first.\n\n"
        f"<research_notes>\n{notes}\n</research_notes>\n\n"
        f"<urls_found_during_search>\n{source_lines or '(none)'}\n</urls_found_during_search>"
    )
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=BETAS,
        fallbacks="default",
        system=STRUCTURE_SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": NICHE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    _check_stop(message, "Scoring")
    text = next(b.text for b in message.content if b.type == "text")
    data = json.loads(text)
    data["niches"].sort(key=lambda n: n["overall_score"], reverse=True)
    return data


def to_markdown(data: dict) -> str:
    out = [f"# Ebook niche research: {data['topic']}", f"_Generated {date.today()}_", "", data["summary"], ""]
    out += ["| # | Niche | Overall | Demand | Low competition | Money |", "|---|---|---|---|---|---|"]
    for i, n in enumerate(data["niches"], 1):
        out.append(f"| {i} | {n['name']} | {n['overall_score']} | {n['demand_score']} | "
                   f"{n['competition_score']} | {n['monetization_score']} |")
    for i, n in enumerate(data["niches"], 1):
        out += ["", f"## {i}. {n['name']}  ({n['overall_score']}/10)", "",
                f"**Reader:** {n['target_reader']}  ", f"**Problem:** {n['problem_solved']}  ",
                f"**Price range:** {n['price_range']}", ""]
        for label, key in [("Evidence", "evidence"), ("Existing competitors", "existing_competitors"),
                           ("Content gaps", "content_gaps"), ("Title ideas", "suggested_titles"),
                           ("Chapter ideas", "chapter_ideas")]:
            if n[key]:
                out += [f"**{label}**", *[f"- {x}" for x in n[key]], ""]
        if n["keywords"]:
            out += [f"**Keywords:** {', '.join(n['keywords'])}", ""]
        out += [f"**Risks:** {n['risks']}", ""]
        if n["sources"]:
            out += ["**Sources**", *[f"- [{s['title']}]({s['url']})" for s in n["sources"]], ""]
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Research ebook niches with Claude.")
    p.add_argument("topic", help='Broad topic to explore, e.g. "personal finance"')
    p.add_argument("--count", type=int, default=5, help="Number of niches to return (default 5)")
    p.add_argument("--audience", help="Optional audience focus, e.g. 'new parents'")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"],
                   help="Research depth/cost (default high)")
    p.add_argument("--max-searches", type=int, default=25, help="Cap on web searches and fetches each")
    p.add_argument("--out", default="reports", help="Output folder (default ./reports)")
    args = p.parse_args()

    client = anthropic.Anthropic()
    try:
        print(f"Researching {args.topic!r}…", file=sys.stderr)
        notes, sources = research(client, args.topic, args.count, args.audience,
                                  args.effort, args.max_searches)
        print("Scoring niches…", file=sys.stderr)
        data = structure(client, args.topic, notes, sources, args.count)
    except BotError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except anthropic.AuthenticationError:
        print("Error: no valid API key. Set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("Error: rate limited by the API; try again shortly.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as e:
        print(f"API error {e.status_code}: {e.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("Error: could not reach the Anthropic API.", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", args.topic.lower()).strip("-") or "topic"
    stem = out_dir / f"{date.today()}-{slug}"
    stem.with_suffix(".json").write_text(json.dumps(data, indent=2))
    stem.with_suffix(".md").write_text(to_markdown(data))
    (out_dir / f"{date.today()}-{slug}-notes.md").write_text(notes + "\n")

    print(f"\nTop niches for {args.topic!r}:")
    for i, n in enumerate(data["niches"], 1):
        print(f"  {i}. {n['name']}  ({n['overall_score']}/10)")
    print(f"\nReport: {stem.with_suffix('.md')}\nData:   {stem.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
