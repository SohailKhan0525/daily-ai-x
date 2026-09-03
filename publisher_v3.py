import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime

import feedparser
from twikit import Client

MAX_POST_LENGTH = 280
HISTORY_FILE = "post_history.json"
# Gemini's current stable Flash model for this API path.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")

ALLOWED = [
    "gemini", "claude", "chatgpt", "openai", "opencode", "openclaw",
    "vibe coding", "vibecoding", "artificial intelligence", "ai",
    "machine learning", "deep learning", "ai agents", "coding",
    "developer tools", "programming", "software", "github", "open source",
    "python", "javascript", "typescript", "gaming", "game dev", "steam",
    "playstation", "xbox", "nintendo", "llm", "generative ai",
]

BLOCKED = [
    "pentagon", "white house", "congress", "senate", "election", "president",
    "politics", "political", "government", "parliament", "minister", "war",
    "military", "army", "navy", "air force", "nato", "sanctions", "ukraine",
    "russia", "israel", "iran", "palestine", "gaza", "china", "india", "pakistan",
    "united states", "u.s.", "usa", "america", "united kingdom", "uk", "canada",
    "australia", "japan", "korea", "france", "germany", "country", "geopolitics",
]

FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "GitHub Blog": "https://github.blog/feed/",
    "Google News AI Tech": "https://news.google.com/rss/search?q=" + urllib.parse.quote(
        '"ChatGPT" OR Gemini OR Claude OR OpenAI OR OpenClaw OR OpenCode OR "vibe coding" OR "machine learning" OR gaming'
    ),
}

CANNED = {
    "game-changer", "game changer", "revolutionizing", "revolutionary",
    "unlock the power", "exciting times", "let that sink in", "the future is here",
    "it's worth noting", "transformative", "seamlessly", "delve into",
}


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def blocked(s):
    low = norm(s).casefold()
    return any(re.search(r"(?<![\w])" + re.escape(x) + r"(?![\w])", low) for x in BLOCKED)


def allowed(s):
    low = norm(s).casefold()
    return bool(low) and not blocked(low) and any(
        re.search(r"(?<![\w])" + re.escape(x) + r"(?![\w])", low) for x in ALLOWED
    )


def title_is_allowed(title):
    # Do not select an unrelated story merely because its summary mentions AI.
    return allowed(title)


def env(name, required=True):
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-200:], f, ensure_ascii=False, indent=2)
        f.write("\n")


def collect_sources():
    items, seen = [], set()
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = norm(entry.get("title"))
                summary = norm(re.sub(r"<[^>]+>", " ", str(entry.get("summary", ""))))[:1400]
                if not title or not title_is_allowed(title):
                    continue
                key = re.sub(r"\W+", " ", title.casefold()).strip()
                if key in seen:
                    continue
                seen.add(key)
                items.append({"source": source, "title": title, "summary": summary})
        except Exception as exc:
            print(f"RSS failed for {source}: {exc}")
    return items


def choose_source(items, history):
    recent = {norm(x).casefold() for x in history[-80:]}
    fresh = [x for x in items if x["title"].casefold() not in recent]
    pool = fresh or items
    if not pool:
        raise RuntimeError("No eligible fresh AI/technology/gaming source found")
    return pool[0]


def gemini_keys():
    keys = [os.getenv("GEMINI_API_KEY", "").strip(), os.getenv("GEMINI_API_KEY_BACKUP", "").strip()]
    return list(dict.fromkeys(k for k in keys if k))


def gemini_call(prompt):
    keys = gemini_keys()
    if not keys:
        raise RuntimeError("No Gemini API key configured")
    last = None
    for index, key in enumerate(keys, 1):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "response_schema": {
                    "type": "OBJECT",
                    "properties": {
                        "post": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["post", "reason"],
                },
                "temperature": 0.85,
            },
        }).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read().decode())
            return json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:
            last = exc
            print(f"Gemini key {index}/{len(keys)} failed: {exc}")
            continue
    raise RuntimeError(f"All Gemini keys failed: {last}")


def funny_slot():
    return datetime.now().astimezone().minute == 45


def make_post(source, history):
    tone = (
        "Make this one genuinely funny or witty about the specific AI/technology topic, while staying accurate."
        if funny_slot() else
        "Make this useful, sharp, conversational, and insight-driven."
    )
    prompt = f"""Write ONE original X post, under 280 characters.

Allowed lane only: Gemini, Claude, ChatGPT, OpenAI, OpenCode, OpenClaw, vibe coding,
AI, machine learning, coding/developer tools, software, technology, or gaming.
Never mention or discuss countries, governments, politics, military, geopolitics,
the Pentagon, or the US/USA/America.

SOURCE — the only factual grounding:
Title: {source['title']}
Summary: {source['summary']}

Recent posts to avoid repeating:
{json.dumps(history[-20:], ensure_ascii=False)}

{tone}
Rules:
- Hook quickly with the specific topic from the title.
- Add one concrete implication, observation, contrast, or joke grounded in the source.
- Do not invent numbers, dates, capabilities, quotes, tests, rankings, prices, or outcomes.
- No URLs, hashtags, bullets, generic engagement bait, or fake personal experience.
- Avoid phrases like game-changer, revolutionary, transformative, exciting times, and let that sink in.
- Do not use first-person claims or unsupported superlatives.
- Return JSON with exactly: post, reason.
"""
    return gemini_call(prompt)


def validate(post, source, history):
    post = norm(post)
    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post too long: {len(post)}")
    if blocked(post) or not allowed(post):
        raise ValueError("Post outside the allowed topic lane")
    if re.search(r"https?://|www\.", post, re.I):
        raise ValueError("URL detected")
    if any(x in post.casefold() for x in CANNED):
        raise ValueError("Canned phrase detected")
    if re.search(r"\b(?:i|i'm|i’ve|i've|my|me|mine)\b", post.casefold()):
        raise ValueError("First-person claim detected")
    if post.casefold() in {x.casefold() for x in history}:
        raise ValueError("Duplicate post")
    title_terms = [x for x in ALLOWED if re.search(r"(?<![\w])" + re.escape(x) + r"(?![\w])", source["title"].casefold())]
    if title_terms and not any(x in post.casefold() for x in title_terms):
        raise ValueError("Post lost the selected topic")
    return post


async def main():
    live = os.getenv("POST_TO_X", "false").lower() == "true"
    if not live:
        print("DRY RUN: POST_TO_X is not true")

    client = Client("en-US")
    client.set_cookies({"auth_token": env("X_AUTH_TOKEN"), "ct0": env("X_CT0")})
    history = load_history()
    sources = collect_sources()
    source = choose_source(sources, history)
    print(f"SELECTED SOURCE: {source['source']} — {source['title']}")
    print(f"GEMINI KEYS AVAILABLE: {len(gemini_keys())}")
    if live and len(gemini_keys()) < 2:
        print("WARNING: GEMINI_API_KEY_BACKUP is not configured; only the primary key is available.")

    last = None
    for attempt in range(1, 5):
        try:
            result = make_post(source, history)
            post = validate(result.get("post"), source, history)
            print(json.dumps({"attempt": attempt, "post": post, "reason": result.get("reason", "")}, ensure_ascii=False, indent=2))
            if not live:
                return
            tweet = await client.create_tweet(text=post)
            tweet_id = getattr(tweet, "id", None) or getattr(tweet, "id_str", "unknown")
            print(f"POSTED TO X: {tweet_id}")
            history.append(post)
            save_history(history)
            return
        except Exception as exc:
            last = exc
            print(f"POST ATTEMPT {attempt}/4 FAILED: {exc}")
            await asyncio.sleep(min(20 * attempt, 60))

    raise RuntimeError(f"Could not publish a valid post: {last}")


if __name__ == "__main__":
    asyncio.run(main())
