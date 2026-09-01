import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import feedparser
from twikit import Client

FEEDS = {
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "GitHub Blog": "https://github.blog/feed/",
    "Google News Tech": "https://news.google.com/rss/search?q="
    + urllib.parse.quote("AI OR software OR developer tools OR IDE OR programming"),
}

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 240
HISTORY_FILE = os.path.expanduser("~/daily-ai-x-history.json")

CANNED_PHRASES = {
    "game-changer", "game changer", "revolutionizing", "revolutionary",
    "unlock the power", "exciting times", "let that sink in",
    "the future is here", "it's worth noting", "as we navigate",
    "this highlights the importance", "transformative", "seamlessly",
    "delve into",
}

# Keep lane keywords specific. Generic words such as "model", "code", "win",
# "final", and "goal" are intentionally excluded because they create false matches.
TREND_LANES = {
    "ai": [
        "ai", "artificial intelligence", "chatgpt", "openai", "gemini", "claude",
        "anthropic", "grok", "llm", "large language model", "agent", "agents",
        "inference", "multimodal", "generative ai", "machine learning", "deepmind",
        "copilot", "perplexity", "mistral", "qwen", "llama",
    ],
    "vibe_coding": [
        "vibe coding", "vibecoding", "cursor", "windsurf", "lovable", "bolt.new",
        "replit", "claude code", "codex", "copilot coding", "coding agent",
        "code agent", "ai coding", "ai-assisted coding", "ai assisted coding",
    ],
    "developers": [
        "developer", "developers", "programming", "programmer", "coding",
        "github", "gitlab", "git", "vscode", "vs code", "visual studio code",
        "ide", "sdk", "api", "npm", "pypi", "python", "javascript", "typescript",
        "rust", "golang", "java", "kotlin", "swift", "docker", "kubernetes",
        "linux", "compiler", "framework", "database", "devtools", "developer tools",
        "software", "terminal", "cli", "repository", "repo", "open source",
    ],
    "sports": [
        "sports", "football", "soccer", "basketball", "cricket", "tennis",
        "formula 1", "f1", "nfl", "nba", "nhl", "mlb", "ipl", "wpl", "cpl",
        "arsenal", "barcelona", "real madrid", "manchester", "liverpool",
        "chelsea", "ronaldo", "cristiano", "messi", "mbappe", "neymar",
        "alcaraz", "djokovic", "nadal", "sinner", "kohli", "rohit", "bumrah",
        "shubman", "dhoni", "hetmyer", "williams", "retirement", "championship",
        "us open", "wimbledon", "grand slam",
    ],
}

UNSUPPORTED_CLAIM_PATTERNS = [
    r"\bmost effective\b", r"\bmost successful\b", r"\bmost popular\b",
    r"\bbest ever\b", r"\bworst ever\b", r"\bbiggest ever\b", r"\bfirst ever\b",
    r"\bin a decade\b", r"\bin years\b", r"\bfor years\b", r"\bever\b",
    r"\bthe biggest\b", r"\bthe best\b", r"\bthe worst\b", r"\bthe greatest\b",
    r"\bunprecedented\b", r"\bhistoric\b", r"\bmassive\b", r"\bhuge\b",
    r"\bclearly the\b", r"\bby far\b", r"\bnumber one\b", r"\bno one saw\b",
]

NUMBER_RE = re.compile(
    r"(?:[$€£]\s*\d[\d,.]*\s*[kKmMbB]?|\b\d[\d,.]*\s*"
    r"(?:million|billion|thousand|[kKmMbB])\b)"
)


def _env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _keyword_pattern(keyword):
    return re.compile(r"(?<![\w])" + re.escape(keyword.casefold()) + r"(?![\w])")


def _trend_score(name, keywords):
    text = name.casefold()
    score = 0
    for keyword in keywords:
        if _keyword_pattern(keyword).search(text):
            score += 2 if " " in keyword else 1
    return score


def filter_x_trends(x_trends, max_per_lane=8):
    filtered = {lane: [] for lane in TREND_LANES}
    seen = set()

    # Sports gets first refusal so sports stories cannot be classified as tech.
    for source_category in ["sports", "for-you", "news", "trending"]:
        for raw_name in x_trends.get(source_category, []):
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue

            matches = [
                (_trend_score(name, keywords), lane)
                for lane, keywords in TREND_LANES.items()
            ]
            matches = [(score, lane) for score, lane in matches if score > 0]
            if not matches:
                continue

            score, lane = max(matches, key=lambda item: item[0])
            if score < 2 and lane != "sports":
                continue

            filtered[lane].append({
                "name": name,
                "score": score,
                "source": source_category,
            })
            seen.add(key)

    for lane in filtered:
        filtered[lane].sort(
            key=lambda item: (item["score"], item["source"] == "sports"),
            reverse=True,
        )
        filtered[lane] = filtered[lane][:max_per_lane]
    return filtered


async def collect_x_trends(count=30):
    client = Client("en-US")
    client.set_cookies({
        "auth_token": _env("X_AUTH_TOKEN"),
        "ct0": _env("X_CT0"),
    })
    result = {}
    for category in ["trending", "for-you", "news", "sports"]:
        try:
            trends = await client.get_trends(category, count=count, retry=False)
            names = []
            seen = set()
            for trend in trends:
                name = getattr(trend, "name", None) or str(trend)
                name = str(name).strip()
                key = name.casefold()
                if name and key not in seen:
                    names.append(name)
                    seen.add(key)
            result[category] = names
        except Exception as exc:
            print(f"X trends unavailable for {category}: {exc}")
            result[category] = []
    return result


def collect_candidates(limit_per_feed=8):
    candidates = []
    seen_titles = set()

    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit_per_feed]:
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", "")).strip()
            if not title:
                continue

            key = re.sub(r"\W+", " ", title.casefold()).strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary", "")))
            summary = re.sub(r"\s+", " ", summary).strip()[:800]
            candidates.append({
                "source": source,
                "title": title,
                "summary": summary,
                "published": entry.get("published", entry.get("updated", "")),
                "url": link,
            })
    return candidates


def _meaningful_words(text):
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
        "from", "after", "before", "is", "are", "was", "were", "this", "that",
        "it", "its", "has", "have", "had", "as", "at", "by", "be", "been",
        "into", "than", "their", "they", "them", "will", "can", "just", "now",
        "plan", "plans", "news",
    }
    return {
        word for word in re.findall(r"[a-z0-9][a-z0-9'’-]*", text.casefold())
        if len(word) >= 4 and word not in stop
    }


def _topic_has_rss_support(topic, candidates):
    topic_words = _meaningful_words(topic.replace("#", " "))
    if not topic_words:
        return False

    for item in candidates:
        source = f"{item.get('title', '')} {item.get('summary', '')}"
        source_words = _meaningful_words(source)
        if len(topic_words & source_words) >= min(2, len(topic_words)):
            return True
    return False


def _source_text(candidates, filtered_trends):
    lines = []
    for lane, items in filtered_trends.items():
        for item in items:
            lines.append(f"X trend [{lane}]: {item['name']}")
    for item in candidates:
        lines.append(
            f"RSS [{item['source']}]: {item['title']} — {item['summary']}"
        )
    return "\n".join(lines)


def _gemini_request(prompt):
    api_key = _env("GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "OBJECT",
                "properties": {
                    "topic": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                    "post": {"type": "STRING"},
                },
                "required": ["topic", "reason", "post"],
            },
        },
    }).encode("utf-8")

    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))

    text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    result = json.loads(text)
    result["post"] = re.sub(
        r"\s+", " ", str(result.get("post", "")).strip().strip('"')
    )
    return result


def _gemini_verify(prompt):
    api_key = _env("GEMINI_API_KEY")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "OBJECT",
                "properties": {
                    "ok": {"type": "BOOLEAN"},
                    "reason": {"type": "STRING"},
                },
                "required": ["ok", "reason"],
            },
        },
    }).encode("utf-8")

    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    text = payload["candidates"][0]["content"]["parts"][0]["text"].strip()
    return json.loads(text)


def _trend_only_result(topic):
    return {
        "topic": topic,
        "reason": "No matching RSS evidence was found, so this is a trend-only observation.",
        "post": f"{topic} is trending on X. The timeline has chosen its main character again.",
    }


def _best_tech_trend_without_rss(filtered_trends):
    ranked = []
    for lane in ["ai", "vibe_coding", "developers"]:
        for item in filtered_trends.get(lane, []):
            ranked.append((item["score"], item["name"]))
    ranked.sort(reverse=True)
    return ranked[0][1] if ranked else None


def _best_rss_backed_trend(filtered_trends, candidates):
    ranked = []
    for lane in ["ai", "vibe_coding", "developers"]:
        for item in filtered_trends.get(lane, []):
            if _topic_has_rss_support(item["name"], candidates):
                ranked.append((item["score"], item["name"]))
    ranked.sort(reverse=True)
    return ranked[0][1] if ranked else None


def gemini_generate(candidates, history, filtered_trends, forced_topic=None):
    source_text = _source_text(candidates, filtered_trends)
    recent_history = history[-20:]
    forced = (
        f"\nUse this exact X trend as the topic: {forced_topic}\n"
        if forced_topic else ""
    )

    prompt = f"""Write ONE short, funny, natural X post for a personal tech/developer account.

SOURCE MATERIAL — the ONLY factual ground truth:
{source_text}
{forced}
RECENT POSTS:
{json.dumps(recent_history, ensure_ascii=False)}

Rules:
- Only use AI, vibe-coding, or developer topics. Never choose sports.
- Prefer an exact X trend that also has matching RSS evidence.
- If the selected trend has no RSS evidence, make the post meta-only about the trend.
- RSS provides factual context, but never combine unrelated stories.
- Every factual statement must be directly supported by the supplied source material.
- Never invent capabilities, permissions, access, actions, tests, outcomes, motives,
  comparisons, dates, numbers, prices, rankings, anecdotes, or technical details.
- A trend name alone does not prove what a product, model, company, person, or tool can do.
- Humor must be observational rather than invented.
- No first-person claims.
- No politics, celebrity gossip, generic world news, crypto/finance, medical/legal claims.
- No engagement bait.
- No URLs, hashtags, bullets, quotes, or thread formatting.
- Maximum {TARGET_POST_LENGTH} characters.
- Avoid these phrases: {", ".join(sorted(CANNED_PHRASES))}
- Do not use unsupported superlatives such as best, biggest, most effective,
  unprecedented, historic, massive, huge, ever, or by far.

Return JSON with topic, reason, and post only."""
    return _gemini_request(prompt)


def _number_claim_is_supported(post, source_text):
    normalized_source = re.sub(r"\s+", "", source_text).casefold()
    for match in NUMBER_RE.finditer(post):
        token = re.sub(r"\s+", "", match.group(0)).casefold()
        if token not in normalized_source:
            return False
    return True


def validate(result, history, filtered_trends, candidates):
    post = str(result.get("post", "")).strip()
    topic = str(result.get("topic", "")).strip()

    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post is {len(post)} characters; X limit is {MAX_POST_LENGTH}")
    if re.search(r"https?://|www\.", post, re.I):
        raise ValueError("URL detected")
    if any(phrase in post.casefold() for phrase in CANNED_PHRASES):
        raise ValueError("Canned/AI-sounding phrase detected")
    if re.search(r"\b(?:i|i'm|i’ve|i've|my|me|mine)\b", post.casefold()):
        raise ValueError("First-person/personal claim detected")
    for pattern in UNSUPPORTED_CLAIM_PATTERNS:
        if re.search(pattern, post, re.I):
            raise ValueError("Unsupported comparative/historical claim detected")

    source_text = _source_text(candidates, filtered_trends)
    if not _number_claim_is_supported(post, source_text):
        raise ValueError("Unverified number or price detected")

    if post in history:
        raise ValueError("Duplicate post")

    allowed_topics = (
        [item["name"] for items in filtered_trends.values() for item in items]
        + [item["title"] for item in candidates]
    )
    if topic.casefold() not in {x.casefold() for x in allowed_topics}:
        raise ValueError("Topic is not present in supplied sources")

    tech_trend_names = {
        item["name"].casefold()
        for lane in ["ai", "vibe_coding", "developers"]
        for item in filtered_trends.get(lane, [])
    }
    if topic.casefold() in tech_trend_names:
        if not _topic_has_rss_support(topic, candidates):
            if re.search(
                r"\b(?:can|could|will|run|execute|fix|refactor|edit|modify|change|"
                r"deploy|ship|commit|push|access|permissions?)\b",
                post, re.I,
            ):
                raise ValueError("Trend-only post contains an unsupported capability/action claim")

    post_words = _meaningful_words(post)
    source_words = _meaningful_words(source_text)
    if post_words and not (post_words & source_words):
        raise ValueError("Post has no meaningful overlap with supplied sources")


def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def save_history(history):
    directory = os.path.dirname(HISTORY_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=2)


async def post_to_x(text):
    client = Client("en-US")
    client.set_cookies({
        "auth_token": _env("X_AUTH_TOKEN"),
        "ct0": _env("X_CT0"),
    })
    user = await client.user()
    print(f"Authenticated as @{user.screen_name}")
    tweet = await client.create_tweet(text=text)
    return getattr(tweet, "id", "unknown")


def main():
    candidates = collect_candidates()
    if not candidates:
        raise RuntimeError("No RSS feed candidates found")

    x_trends = asyncio.run(collect_x_trends())
    filtered_trends = filter_x_trends(x_trends)
    print("FILTERED X TRENDS:", json.dumps(filtered_trends, ensure_ascii=False, indent=2))

    history = load_history()

    # Prefer a tech trend with RSS support. If none exists, use a tech-only
    # trend fallback rather than drifting into sports or inventing context.
    forced_topic = _best_rss_backed_trend(filtered_trends, candidates)
    trend_only = False
    if not forced_topic:
        forced_topic = _best_tech_trend_without_rss(filtered_trends)
        trend_only = forced_topic is not None

    if not forced_topic:
        raise RuntimeError("No relevant AI/vibe-coding/developer X trend found")

    for attempt in range(1, 4):
        try:
            if trend_only:
                result = _trend_only_result(forced_topic)
            else:
                result = gemini_generate(
                    candidates, history, filtered_trends, forced_topic=forced_topic
                )

            validate(result, history, filtered_trends, candidates)

            verification = _gemini_verify(
                f"""Strictly fact-check this proposed X post.

SOURCE MATERIAL:
{_source_text(candidates, filtered_trends)}

POST:
{result['post']}

Return JSON only:
{{"ok": true/false, "reason": "short reason"}}

Reject any unsupported factual claim, including invented capabilities, access,
actions, tests, outcomes, numbers, rankings, motives, or technical details.
A trend-only observation about the fact that something is trending is allowed."""
            )
            if not verification.get("ok"):
                raise ValueError(
                    f"Factuality gate rejected post: "
                    f"{verification.get('reason', 'unsupported claim')}"
                )
            break
        except (ValueError, json.JSONDecodeError, KeyError, urllib.error.URLError) as exc:
            print(f"WRITER REJECTED ATTEMPT {attempt}: {exc}")
            if attempt == 3:
                raise RuntimeError(
                    f"Could not produce a valid post after 3 attempts: {exc}"
                ) from exc

    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "attempt": attempt,
        "character_count": len(result["post"]),
        **result,
    }, ensure_ascii=False, indent=2))

    if os.getenv("POST_TO_X", "false").casefold() != "true":
        print("DRY RUN: set POST_TO_X=true only when ready to publish.")
        return

    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
