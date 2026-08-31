import json
import os
from datetime import datetime, timezone

import feedparser
from google import genai


FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
}


def collect_candidates(limit_per_feed=8):
    candidates = []
    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_feed]:
            candidates.append(
                {
                    "source": source,
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", "").strip(),
                    "summary": entry.get("summary", "").strip()[:800],
                }
            )
    return candidates


def choose_and_write(candidates):
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    prompt = f"""
You are the editorial brain for a daily X account focused on AI, software development,
and interesting technology.

Today's candidate stories:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Pick ONE topic with the strongest potential for an original, useful X post.
Do not simply summarize the headline. Look for a surprising development, useful insight,
tradeoff, disagreement, or strong opinion.

Return ONLY valid JSON with:
{{
  "topic": "...",
  "reason": "...",
  "post": "..."
}}

Rules for post:
- Under 280 characters.
- Sound like a thoughtful human, not a marketing account.
- No fake facts.
- No hashtags unless genuinely useful.
- No emojis unless they add meaning.
- Do not claim personal experience you don't have.
- Do not include a URL yet.
"""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json\n", "", 1).strip()
    return json.loads(text)


def main():
    candidates = collect_candidates()
    result = choose_and_write(candidates)
    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
