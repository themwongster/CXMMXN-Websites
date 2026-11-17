# Ebook Niche Research Bot

Give it a broad topic and it searches the web for profitable ebook sub-niches, scores them, and writes a report.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # or: ant auth login
```

## Usage

```bash
python niche_bot.py "personal finance"
python niche_bot.py "gardening" --count 10 --audience "retirees"
python niche_bot.py "dog training" --effort medium --max-searches 15   # cheaper/faster
```

Output goes to `./reports/` (change it with `--out`):

- `YYYY-MM-DD-<topic>.md`: ranked table plus a section per niche (reader, problem, evidence, competitors, content gaps, title ideas, chapter ideas, keywords, price range, risks, sources)
- `YYYY-MM-DD-<topic>.json`: the same data as JSON for spreadsheets or other tools
- `YYYY-MM-DD-<topic>-notes.md`: the raw research notes

## How it works

1. **Research.** Claude (`claude-opus-5-5`) uses web search and web fetch to look at Amazon/Kindle categories, competing titles and their reviews, Reddit/forums, and price points. Long research runs that pause get resumed automatically.
2. **Scoring.** A second call turns the notes into structured JSON. Each niche gets four scores from 1 to 10: demand, competition (10 = easy to enter), monetization, and overall.

If the main model declines a request, it is retried on a fallback model automatically (`fallbacks: "default"`).

## Cost

One run makes up to `--max-searches` searches and fetches (25 each by default) plus two model calls. Lower `--effort` and `--max-searches` to spend less.
