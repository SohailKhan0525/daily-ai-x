import asyncio
import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

import feedparser
from twikit import Client

MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 245
HISTORY_FILE = "post_history.json"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# The account's permanent content lane.
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

BLOCKED_TERMS = [
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
    "unlock the power", "exciting times", "let that sink in", "the future is here",
    "it's worth noting", "as we navigate", "this highlights the importance",
    "transformative", "seamlessly", "delve into",
}

SUPERLATIVE_PATTERNS = [
    r"\bmost effective\b", r"\bmost successful\b", r"\bmost popular\b",
    r"\bbest ever\b", r"\bworst ever\b", r"\bbiggest ever\b", r"\bfirst ever\b",
    r"\bunprecedented\b", r"\bhistoric\b", r"\bby far\b", r"\bnumber one\b",
]

NUMBER_RE = re.compile(
    r"(?<![\w])(?:[$€£]\s*)?(?:\d+(?:\.\d+)?)(?:\s*(?:million|billion|thousand))?(?![\w])",
    re.I,
)


def env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def norm(text):
    return re.sub(r"\s+", " ", str(text or "").strip())


def contains_blocked(text):
    low = norm(text).casefold()
    return any(
        re.search(r"(?<![\w])" + re.escape(term.casefold()) + r"(?![\w])", low)
        for term in BLOCKED_TERMS
    )


def keyword_score(text, keywords):
    low = norm(text).casefold()
    score = 0
    for kw in keywords:
        if re.search(r"(?<![\w])" + re.escape(kw.casefold()) + r"(?![\w])", low):
            score += 2 if " " in kw else 1
    return score


def allowed_topic(text):
    text = norm(text)
    return bool(text) and not contains_blocked(text) and any(
        keyword_score(text, kws) > 0 for kws in TOPIC_GROUPS.values()
    )


def topic_score(text):
    return max(keyword_score(text, kws) for kws in TOPIC_GROUPS.values())


def matching_topics(text):
    low = norm(text).casefold()
    found = []
    for group, keywords in TOPIC_GROUPS.items():
        for kw in keywords:
            if re.search(r"(?<![\w])" + re.escape(kw.casefold()) + r"(?![\w])", low):
                found.append((kw, group))
    return found


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
    """Use Twikit's trend method when available; gracefully fall back if X has changed it."""
    get_trends = getattr(client, "get_trends", None)
    if get_trends:
        try:
            trends = await get_trends("trending", count=count, retry=False)
            names = [norm(getattr(t, "name", t)) for t in (trends or [])]
            names = [x for x in names if x]
            if names:
                print(f"Twikit trends: {len(names)} live trends")
                return names
        except Exception as exc:
            print(f"Twikit get_trends unavailable: {exc}")
    return []


async def twikit_momentum(client):
    """Use one Top search to find which approved topics currently have attention."""
    query = (
        '"ChatGPT" OR Gemini OR Claude OR OpenAI OR OpenClaw OR OpenCode '
        'OR "vibe coding" OR "machine learning" OR gaming'
    )
    try:
        tweets = await client.search_tweet(query, "Top", 20)
    except Exception as exc:
        print(f"Twikit momentum search failed: {exc}")
        return []

    scores = {}
    examples = {}
    for tweet in tweets or []:
        text = norm(getattr(tweet, "text", None) or getattr(tweet, "full_text", ""))
        if not text or contains_blocked(text):
            continue
        matches = matching_topics(text)
        if not matches:
            continue
        likes = max(0, int(getattr(tweet, "favorite_count", 0) or 0))
        replies = max(0, int(getattr(tweet, "reply_count", 0) or 0))
        reposts = max(0, int(getattr(tweet, "retweet_count", 0) or 0))
        views = max(0, int(getattr(tweet, "view_count", 0) or 0))
        # Replies/reposts are deliberately weighted more heavily than passive views.
        engagement = (
            1.0 * math.log1p(likes)
            + 1.8 * math.log1p(replies)
            + 1.8 * math.log1p(reposts)
            + 0.35 * math.log1p(views)
        )
        for keyword, _group in matches:
            scores[keyword] = scores.get(keyword, 0.0) + engagement
            examples.setdefault(keyword, text[:220])

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    result = [
        {"keyword": keyword, "momentum": round(score, 3), "example": examples[keyword]}
        for keyword, score in ranked[:15]
    ]
    print("TWIKIT MOMENTUM:", json.dumps(result[:10], ensure_ascii=False, indent=2))
    return result


def collect_rss(limit_per_feed=10):
    candidates = []
    seen = set()
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            print(f"RSS failed for {source}: {exc}")
            continue
        for entry in feed.entries[:limit_per_feed]:
            title = norm(entry.get("title"))
            if not title:
                continue
            key = re.sub(r"\W+", " ", title.casefold()).strip()
            if key in seen:
                continue
            seen.add(key)
            summary = norm(re.sub(r"<[^>]+>", " ", str(entry.get("summary", ""))))[:1000]
            if contains_blocked(f"{title} {summary}") or not allowed_topic(f"{title} {summary}"):
                continue
            candidates.append({
                "source": source,
                "title": title,
                "summary": summary,
                "published": entry.get("published", entry.get("updated", "")),
            })
    return candidates


def trend_candidates(names):
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
        })
    return ranked


def source_text(trends, momentum, rss):
    lines = []
    for item in trends[:20]:
        lines.append(f"X TREND rank={item['trend_rank']} topic={item['name']}")
    for item in momentum[:15]:
        lines.append(f"X MOMENTUM keyword={item['keyword']} score={item['momentum']}")
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
            "temperature": 0.75,
        },
    }).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    return json.loads(payload["candidates"][0]["content"]["parts"][0]["text"])


def generate_post(trends, momentum, rss, history):
    material = source_text(trends, momentum, rss)
    prompt = f"""Write ONE original X post for a technology-focused personal account.

ALLOWED ONLY: Gemini, Claude, ChatGPT, OpenAI, OpenCode, OpenClaw, vibe coding,
artificial intelligence, machine learning, coding/developer tools, software,
technology, or gaming.

ABSOLUTELY EXCLUDE: the Pentagon, US/USA/America, any country, country-specific
news, governments, elections, politics, military, wars, or geopolitics.

LIVE SOURCE MATERIAL — ONLY THIS MATERIAL may support factual claims:
{material}

RECENT POSTS TO AVOID REPEATING:
{json.dumps(history[-25:], ensure_ascii=False)}

Choose the source with the strongest combination of current X trend rank,
X momentum, and fresh RSS relevance. X trend/momentum is a signal for what
people are discussing; RSS is the grounding source for factual claims.

Writing rules:
- Make the first sentence a strong, specific hook that names the relevant product/topic.
- Add one useful implication, tension, comparison, or observation supported by the source.
- A short natural question is allowed only when it genuinely follows from the source;
  never use "What do you think?" or generic engagement bait.
- Keep it conversational and human, not corporate or news-anchor style.
- Never invent facts, numbers, prices, dates, tests, capabilities, access, motives,
  rankings, comparisons, or outcomes.
- Never claim personal experience or use first-person claims.
- No URLs, no hashtags, no bullets, no copied quotations.
- Avoid canned phrases: {", ".join(sorted(CANNED_PHRASES))}
- Avoid unsupported superlatives such as best, biggest, most effective,
  unprecedented, historic, or by far.
- Keep it under {TARGET_POST_LENGTH} characters.
- The JSON "topic" must copy the chosen source title/topic verbatim.

Return JSON: topic, reason, post."""
    result = gemini_call(prompt)
    result["post"] = norm(str(result.get("post", "")).strip('"'))
    return result


def topic_is_grounded(topic, candidates):
    low = norm(topic).casefold()
    if not low:
        return False
    for candidate in candidates:
        other = norm(candidate).casefold()
        if low == other:
            return True
        ratio = SequenceMatcher(None, low, other).ratio()
        shorter = min(len(low), len(other))
        if ratio >= 0.72 or (shorter >= 18 and (low in other or other in low)):
            return True
    return False


def validate(result, trends, momentum, rss, history):
    post = norm(result.get("post"))
    topic = norm(result.get("topic"))
    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post too long: {len(post)}")
    if re.search(r"https?://|www\.", post, re.I):
        raise ValueError("URL detected")
    if any(x in post.casefold() for x in CANNED_PHRASES):
        raise ValueError("Canned phrase detected")
    if re.search(r"\b(?:i|i'm|i’ve|i've|my|me|mine)\b", post.casefold()):
        raise ValueError("First-person claim detected")
    if any(re.search(pattern, post, re.I) for pattern in SUPERLATIVE_PATTERNS):
        raise ValueError("Unsupported superlative detected")
    if contains_blocked(post) or contains_blocked(topic):
        raise ValueError("Blocked geopolitical/country term detected")
    if post in history:
        raise ValueError("Duplicate post")

    candidates = [x["name"] for x in trends]
    candidates += [x["keyword"] for x in momentum]
    candidates += [x["title"] for x in rss]
    if not topic_is_grounded(topic, candidates):
        raise ValueError("Generated topic was not grounded in supplied source material")
    if not allowed_topic(topic):
        raise ValueError("Generated topic is outside the allowed topic set")

    post_numbers = {x.casefold().replace(" ", "") for x in NUMBER_RE.findall(post)}
    source_numbers = {
        x.casefold().replace(" ", "")
        for x in NUMBER_RE.findall(source_text(trends, momentum, rss))
    }
    if not post_numbers.issubset(source_numbers):
        raise ValueError("Unverified number or price detected")
    return post


async def main():
    client = Client("en-US")
    client.set_cookies({"auth_token": env("X_AUTH_TOKEN"), "ct0": env("X_CT0")})

    raw_trends = await twikit_global_trends(client)
    trends = trend_candidates(raw_trends)
    momentum = await twikit_momentum(client)
    rss = collect_rss()

    print("TWIKIT ELIGIBLE TRENDS:", json.dumps(trends[:12], ensure_ascii=False, indent=2))
    print("RSS ELIGIBLE TOPICS:", json.dumps(rss[:12], ensure_ascii=False, indent=2))

    if not trends and not momentum and not rss:
        raise RuntimeError("No eligible AI/coding/gaming signals found")

    history = load_history()
    last_error = None
    for attempt in range(1, 4):
        try:
            result = generate_post(trends, momentum, rss, history)
            post = validate(result, trends, momentum, rss, history)
            output = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "attempt": attempt,
                "topic": result["topic"],
                "reason": result["reason"],
                "post": post,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))

            if os.getenv("POST_TO_X", "false").lower() != "true":
                print("DRY RUN: POST_TO_X is not true")
                return

            tweet = await client.create_tweet(text=post)
            tweet_id = getattr(tweet, "id", None) or getattr(tweet, "id_str", "unknown")
            print(f"POSTED TO X: {tweet_id}")

            history.append(post)
            save_history(history)
            return
        except Exception as exc:
            last_error = exc
            print(f"WRITER REJECTED ATTEMPT {attempt}: {exc}")

    raise RuntimeError(f"Could not produce a valid post after 3 attempts: {last_error}")


if __name__ == "__main__":
    asyncio.run(main())
