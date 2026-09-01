import asyncio
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import feedparser
from twikit import Client

MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 240
HISTORY_FILE = os.path.expanduser("~/daily-ai-x-history.json")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# ONLY these subject areas are eligible.
TOPIC_GROUPS = {
    "ai": [
        "artificial intelligence", "ai", "chatgpt", "openai", "gemini", "claude",
        "anthropic", "grok", "llm", "large language model", "machine learning",
        "deep learning", "generative ai", "ai agent", "ai agents", "deepmind",
    ],
    "coding": [
        "vibe coding", "vibecoding", "opencode", "openclaw", "claude code",
        "codex", "cursor", "windsurf", "lovable", "replit", "bolt",
        "coding agent", "code agent", "ai coding", "developer tools",
        "programming", "developer", "developers", "github", "git", "vscode",
        "visual studio code", "ide", "sdk", "api", "python", "javascript",
        "typescript", "rust", "golang", "docker", "kubernetes", "open source",
        "terminal", "cli", "software",
    ],
    "gaming": [
        "gaming", "game", "games", "steam", "playstation", "xbox", "nintendo",
        "pc gaming", "indie game", "game dev", "game development",
    ],
}

# Keep politics/geopolitics/country-specific material out of the content lane.
BLOCKED_TERMS = [
    "pentagon", "white house", "congress", "senate", "election", "president",
    "politics", "political", "government", "parliament", "minister",
    "war", "military", "army", "navy", "air force", "nato", "sanctions",
    "ukraine", "russia", "israel", "iran", "palestine", "gaza", "china",
    "india", "pakistan", "united states", "u.s.", "usa", "america",
    "united kingdom", " uk ", "canada", "australia", "japan", "korea",
    "france", "germany", "country", "geopolitics",
]

FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "GitHub Blog": "https://github.blog/feed/",
    "Google News Tech": (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(
            '"ChatGPT" OR Gemini OR Claude OR OpenAI OR "vibe coding" '
            'OR OpenCode OR OpenClaw OR gaming OR "machine learning"'
        )
    ),
}

CANNED_PHRASES = {
    "game-changer", "game changer", "revolutionizing", "revolutionary",
    "unlock the power", "exciting times", "let that sink in",
    "the future is here", "it's worth noting", "as we navigate",
    "this highlights the importance", "transformative", "seamlessly",
    "delve into",
}

SUPERLATIVE_PATTERNS = [
    r"\bmost effective\b", r"\bmost successful\b", r"\bmost popular\b",
    r"\bbest ever\b", r"\bworst ever\b", r"\bbiggest ever\b", r"\bfirst ever\b",
    r"\bunprecedented\b", r"\bhistoric\b", r"\bby far\b", r"\bnumber one\b",
]

NUMBER_RE = re.compile(r"\d|[$€£]|\b(?:million|billion|thousand)\b", re.I)


def env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip())


def keyword_score(text, keywords):
    text = text.casefold()
    score = 0
    for kw in keywords:
        if re.search(r"(?<![\w])" + re.escape(kw.casefold()) + r"(?![\w])", text):
            score += 2 if " " in kw else 1
    return score


def allowed_topic(name):
    text = norm(name)
    if not text:
        return False
    low = text.casefold()
    if any(term in low for term in BLOCKED_TERMS):
        return False
    return max(keyword_score(text, kws) for kws in TOPIC_GROUPS.values()) > 0


def topic_score(name):
    text = norm(name)
    return max(keyword_score(text, kws) for kws in TOPIC_GROUPS.values())


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=2)


async def twikit_global_trends(client, count=50):
    """Use Twikit's live trend API, with a global library-native fallback."""
    trends = []
    get_trends = getattr(client, "get_trends", None)
    if get_trends:
        try:
            trends = await get_trends(
                "trending",
                count=count,
                retry=False,
                additional_request_params={"candidate_source": "trends"},
            )
        except TypeError:
            try:
                trends = await get_trends("trending", count=count, retry=False)
            except Exception as exc:
                print(f"Twikit get_trends failed: {exc}")
        except Exception as exc:
            print(f"Twikit get_trends failed: {exc}")

    if trends:
        return [norm(getattr(t, "name", t)) for t in trends if norm(getattr(t, "name", t))]

    # WOEID 1 is worldwide. This avoids choosing a country/region.
    try:
        place = await client.get_place_trends(1)
        raw = getattr(place, "trends", None) or (
            place.get("trends", []) if isinstance(place, dict) else []
        )
        names = [norm(getattr(t, "name", t)) for t in raw]
        return [name for name in names if name]
    except Exception as exc:
        print(f"Twikit global place trends failed: {exc}")
        return []


async def collect_x_topics(client):
    names = await twikit_global_trends(client)
    ranked = []
    seen = set()
    for rank, name in enumerate(names, start=1):
        key = name.casefold()
        if key in seen or not allowed_topic(name):
            continue
        seen.add(key)
        ranked.append({
            "name": name,
            "trend_rank": rank,
            "score": topic_score(name),
            "source": "twikit_global_trends",
        })
    ranked.sort(key=lambda item: (item["score"], -item["trend_rank"]), reverse=True)
    return ranked


def collect_rss(limit_per_feed=8):
    candidates = []
    seen = set()
    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_feed]:
            title = norm(entry.get("title"))
            if not title:
                continue
            key = re.sub(r"\W+", " ", title.casefold()).strip()
            if key in seen:
                continue
            seen.add(key)
            summary = norm(re.sub(r"<[^>]+>", " ", str(entry.get("summary", ""))))[:900]
            if not allowed_topic(f"{title} {summary}"):
                continue
            candidates.append({
                "source": source,
                "title": title,
                "summary": summary,
                "published": entry.get("published", entry.get("updated", "")),
            })
    return candidates


def source_text(x_topics, rss):
    lines = []
    for item in x_topics[:20]:
        lines.append(f"X TREND rank={item['trend_rank']} topic={item['name']}")
    for item in rss[:25]:
        lines.append(
            f"RSS source={item['source']} title={item['title']} summary={item['summary']}"
        )
    return "\n".join(lines)


def gemini_call(prompt):
    key = env("GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={key}"
    )
    schema = {
        "type": "OBJECT",
        "properties": {
            "topic": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "post": {"type": "STRING"},
        },
        "required": ["topic", "reason", "post"],
    }
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": schema,
            "temperature": 0.8,
        },
    }).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    return json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])


def generate_post(x_topics, rss, history):
    material = source_text(x_topics, rss)
    prompt = f"""Write ONE natural, concise X post for a personal technology account.

Allowed subject areas ONLY:
Gemini, Claude, ChatGPT, OpenAI, OpenCode, OpenClaw, vibe coding,
technology, artificial intelligence, machine learning, coding/developer
tools, software, or gaming.

Never write about:
Pentagon, US/USA/America, any country, elections, governments, politics,
military, geopolitics, wars, or country-specific news.

LIVE SOURCE MATERIAL — factual ground truth:
{material}

RECENT POSTS:
{json.dumps(history[-20:], ensure_ascii=False)}

Rules:
- Pick the strongest CURRENT technology/AI/coding/gaming topic from the source material.
- Prefer a higher-ranked Twikit X trend when it is relevant.
- If an X trend has no article attached, only make an observation about the trend itself.
- Never invent facts, numbers, prices, dates, tests, capabilities, access, motives,
  rankings, comparisons, or outcomes.
- No first-person claims.
- No hashtags, URLs, bullets, quotes, or engagement bait.
- Humor can be dry/observational, but must not invent a story.
- Keep it under {TARGET_POST_LENGTH} characters.
- Avoid canned phrases: {", ".join(sorted(CANNED_PHRASES))}
- Avoid unsupported superlatives such as best, biggest, most effective,
  unprecedented, historic, or by far.
- Numbers are allowed only when directly present in the source material.
Return JSON with topic, reason, post."""
    result = gemini_call(prompt)
    result["post"] = norm(str(result.get("post", "")).strip('"'))
    return result


def validate(result, x_topics, rss, history):
    post = norm(result.get("post"))
    topic = norm(result.get("topic"))
    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post too long: {len(post)}")
    if re.search(r"https?://|www\.", post, re.I):
        raise ValueError("URL detected")
    if any(p in post.casefold() for p in CANNED_PHRASES):
        raise ValueError("Canned phrase detected")
    if re.search(r"\b(?:i|i'm|i’ve|i've|my|me|mine)\b", post.casefold()):
        raise ValueError("First-person claim detected")
    if any(re.search(p, post, re.I) for p in SUPERLATIVE_PATTERNS):
        raise ValueError("Unsupported superlative detected")
    if any(term in post.casefold() for term in BLOCKED_TERMS):
        raise ValueError("Blocked geopolitical/country term detected")
    if post in history:
        raise ValueError("Duplicate post")

    allowed = {x["name"].casefold() for x in x_topics}
    allowed.update(x["title"].casefold() for x in rss)
    if topic.casefold() not in allowed:
        raise ValueError("Generated topic was not in supplied source material")
    if not allowed_topic(topic):
        raise ValueError("Generated topic is outside the allowed topic set")

    # If a generated number/price is present, it must exist in the supplied sources.
    post_numbers = NUMBER_RE.findall(post)
    if post_numbers:
        source_numbers = NUMBER_RE.findall(source_text(x_topics, rss))
        source_number_set = {x.casefold() for x in source_numbers}
        if any(n.casefold() not in source_number_set for n in post_numbers):
            raise ValueError("Unverified number or price detected")
    return post


async def main():
    client = Client("en-US")
    client.set_cookies({
        "auth_token": env("X_AUTH_TOKEN"),
        "ct0": env("X_CT0"),
    })

    x_topics = await collect_x_topics(client)
    rss = collect_rss()
    print("TWIKIT ELIGIBLE TRENDS:", json.dumps(x_topics[:12], ensure_ascii=False, indent=2))
    print("RSS ELIGIBLE TOPICS:", json.dumps(rss[:12], ensure_ascii=False, indent=2))

    if not x_topics and not rss:
        raise RuntimeError("No eligible AI/coding/gaming topics found")

    history = load_history()
    last_error = None
    for attempt in range(1, 4):
        try:
            result = generate_post(x_topics, rss, history)
            post = validate(result, x_topics, rss, history)
            output = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "attempt": attempt,
                "topic": result["topic"],
                "reason": result["reason"],
                "post": post,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))

            if os.getenv("POST_TO_X", "false").lower() == "true":
                tweet = await client.create_tweet(text=post)
                print(f"POSTED TO X: {getattr(tweet, 'id', 'unknown')}")
            else:
                print("DRY RUN: set POST_TO_X=true to publish.")

            history.append(post)
            save_history(history)
            return
        except Exception as exc:
            last_error = exc
            print(f"WRITER REJECTED ATTEMPT {attempt}: {exc}")

    raise RuntimeError(f"Could not produce a valid post after 3 attempts: {last_error}")


if __name__ == "__main__":
    asyncio.run(main())
