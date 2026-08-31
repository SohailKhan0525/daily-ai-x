import json
import os
import re
import urllib.request
from datetime import datetime, timezone

import feedparser
from twikit import Client

FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
}

GEMINI_MODEL = "gemini-3.6-flash"
MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 240
HISTORY_FILE = os.path.expanduser("~/daily-ai-x-history.json")


def collect_candidates(limit_per_feed=8):
    candidates = []
    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_feed]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:800]
            if title:
                candidates.append({"source": source, "title": title, "url": link, "summary": summary})
    return candidates


def gemini_generate(candidates):
    api_key = os.environ["GEMINI_API_KEY"]
    prompt = f"""You write one natural X post for a personal account about AI, software, developer tools, startups and technology.

Candidate stories:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Pick one genuinely interesting current topic. Prefer something surprising, useful, relatable or likely to make a developer smile. Do not simply rewrite a headline.

Write ONE funny, sharp, conversational post. Dry humor, playful sarcasm, or developer humor are welcome. Do not force a joke.

Rules:
- Maximum {TARGET_POST_LENGTH} characters.
- Plain text. No URL.
- No thread, title, list, or hashtags.
- No marketing/corporate tone.
- Avoid generic AI-sounding phrases: game-changer, revolutionizing, exciting times, unlock the power, today's rapidly evolving, let that sink in, etc.
- No fake personal experience and no invented facts.
- No engagement bait such as 'Agree?' or 'Thoughts?'.
- Do not manufacture controversy.
- Sound like a real developer posting casually, not like an AI assistant.

Return ONLY JSON: {{"topic":"...","reason":"...","post":"..."}}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json\n", "", 1).strip()
    result = json.loads(text)
    result["post"] = re.sub(r"\s+", " ", result["post"].strip().strip('"'))
    return result


def validate(post, history):
    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post is {len(post)} characters; X limit is {MAX_POST_LENGTH}")
    if "http://" in post.lower() or "https://" in post.lower() or "www." in post.lower():
        raise ValueError("URL detected")
    canned = ["game-changer", "revolutionizing", "unlock the power", "exciting times", "let that sink in", "the future is here", "in today's rapidly evolving"]
    if any(x in post.lower() for x in canned):
        raise ValueError("Canned phrase detected")
    if post in history:
        raise ValueError("Duplicate post")


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=2)


async def post_to_x(text):
    client = Client("en-US")
    client.set_cookies({
        "auth_token": os.environ["X_AUTH_TOKEN"],
        "ct0": os.environ["X_CT0"],
    })
    user = await client.user()
    print(f"Authenticated as @{user.screen_name}")
    tweet = await client.create_tweet(text=text)
    return getattr(tweet, "id", "unknown")


def main():
    candidates = collect_candidates()
    if not candidates:
        raise RuntimeError("No feed candidates found")
    history = load_history()
    result = gemini_generate(candidates)
    validate(result["post"], history)
    print(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), **result}, ensure_ascii=False, indent=2))

    if os.environ.get("POST_TO_X", "false").lower() != "true":
        print("DRY RUN: set POST_TO_X=true only when you are ready to publish.")
        return

    import asyncio
    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
