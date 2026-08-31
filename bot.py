import json
import os
import re
from datetime import datetime, timezone

import feedparser
from google import genai


FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
}

MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 240
GEMINI_MODEL = "gemini-3.6-flash"


def collect_candidates(limit_per_feed=8):
    candidates = []
    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_feed]:
            candidates.append({
                "source": source,
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", "").strip(),
                "summary": re.sub(r"<[^>]+>", " ", entry.get("summary", ""))[:800].strip(),
            })
    return candidates


def clean_post(post: str) -> str:
    post = post.strip().strip('"')
    return re.sub(r"\s+", " ", post)


def validate_post(post: str) -> list[str]:
    problems = []
    if not post:
        problems.append("empty")
    if len(post) > MAX_POST_LENGTH:
        problems.append(f"over_{MAX_POST_LENGTH}_characters")
    if "http://" in post.lower() or "https://" in post.lower() or "www." in post.lower():
        problems.append("contains_url")
    if post.startswith("#") and post.count("#") > 1:
        problems.append("hashtag_heavy")
    canned = [
        "in today's rapidly evolving", "game-changer", "revolutionizing",
        "unlock the power", "exciting times", "let that sink in",
        "the future is here", "here's the thing", "it's worth noting",
    ]
    lower = post.lower()
    if any(phrase in lower for phrase in canned):
        problems.append("canned_ai_phrase")
    return problems


def choose_and_write(candidates):
    api_key = os.environ["GEMINI_API_KEY"]
    client = genai.Client(api_key=api_key)
    prompt = f"""
You are the editorial brain for a personal X account about AI, software development,
developer tools, startups and interesting technology.

Today's candidate stories:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Pick ONE topic that is genuinely interesting right now. Prefer active conversations,
disagreement, surprising developments, or relatable developer experiences. Do not merely
rewrite a headline.

Write ONE funny, sharp, natural X post. It should sound like a smart developer noticed
something funny or absurd and decided to post it. Humor may be dry, playful, sarcastic,
or lightly self-deprecating. Never force a joke.

STRICT RULES:
- Maximum 240 characters; X's standard post limit is 280 characters.
- Plain text and no URL.
- No thread, title, or numbered list.
- No corporate/marketing tone or generic motivational language.
- Avoid canned AI phrases such as game-changer, revolutionizing, exciting times,
  today's rapidly evolving, unlock the power, let that sink in, etc.
- No fake personal experiences or invented facts.
- No engagement bait such as Agree?, Thoughts?, or Who's with me?
- Prefer zero hashtags and rare/no emojis.
- Be specific, concise, conversational, and original.
- Do not manufacture controversy.

Return ONLY valid JSON:
{{"topic":"...","reason":"...","post":"..."}}
"""
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json\n", "", 1).strip()
    result = json.loads(text)
    result["post"] = clean_post(result["post"])
    problems = validate_post(result["post"])
    if problems:
        raise ValueError(f"Post validation failed: {', '.join(problems)}")
    return result


def main():
    candidates = collect_candidates()
    if not candidates:
        raise RuntimeError("No trend candidates were collected")
    result = choose_and_write(candidates)
    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
