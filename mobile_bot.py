import asyncio
import json
import os
import re
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

GEMINI_MODEL = "gemini-3.6-flash"
MAX_POST_LENGTH = 280
TARGET_POST_LENGTH = 240
HISTORY_FILE = os.path.expanduser("~/daily-ai-x-history.json")

CANNED_PHRASES = [
    "game-changer",
    "game changer",
    "revolutionizing",
    "revolutionary",
    "unlock the power",
    "exciting times",
    "let that sink in",
    "the future is here",
    "in today's rapidly evolving",
    "in the rapidly evolving",
    "it's worth noting",
    "as we navigate",
    "this highlights the importance",
    "transformative",
    "seamlessly",
    "delve into",
]

# Only these four lanes are allowed to reach the writer.
TREND_LANES = {
    "ai": [
        "ai", "artificial intelligence", "chatgpt", "openai", "gemini", "claude",
        "anthropic", "grok", "llm", "large language model", "model", "models",
        "agent", "agents", "inference", "multimodal", "generative ai", "machine learning",
        "deepmind", "copilot", "perplexity", "mistral", "qwen", "llama",
    ],
    "vibe_coding": [
        "vibe coding", "vibecoding", "cursor", "windsurf", "lovable", "bolt.new",
        "replit", "claude code", "codex", "copilot coding", "coding agent", "code agent",
        "ai coding", "ai code", "ai-assisted coding", "ai assisted coding",
    ],
    "developers": [
        "developer", "developers", "programming", "programmer", "coding", "code",
        "github", "gitlab", "git", "vscode", "vs code", "visual studio code", "ide",
        "sdk", "api", "npm", "pypi", "python", "javascript", "typescript", "rust",
        "golang", "java", "kotlin", "swift", "docker", "kubernetes", "linux",
        "compiler", "framework", "database", "devtools", "developer tools", "software",
        "terminal", "cli", "repository", "repo", "open source",
    ],
    "sports": [
        "sports", "football", "soccer", "basketball", "cricket", "tennis", "formula 1",
        "f1", "nfl", "nba", "nhl", "mlb", "ipl", "wpl", "cpl", "arsenal", "barcelona",
        "real madrid", "manchester", "liverpool", "chelsea", "ronaldo", "cristiano",
        "messi", "mbappe", "neymar", "alcaraz", "djokovic", "nadal", "sinner",
        "kohli", "rohit", "bumrah", "shubman", "dhoni", "hetmyer", "williams",
        "goal", "match", "win", "loss", "retirement", "championship", "final",
        "us open", "wimbledon", "grand slam",
    ],
}


def _trend_score(name, keywords):
    text = name.casefold()
    score = 0
    for keyword in keywords:
        if keyword in text:
            score += 2 if " " in keyword else 1
    return score


def filter_x_trends(x_trends, max_per_lane=8):
    """Keep only relevant live X trends for the account's four content lanes."""
    filtered = {lane: [] for lane in TREND_LANES}
    seen = set()

    # Prefer X's dedicated sports bucket when assigning sports trends.
    ordered_categories = ["sports", "for-you", "news", "trending"]
    for source_category in ordered_categories:
        for raw_name in x_trends.get(source_category, []):
            name = str(raw_name).strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue

            matches = []
            for lane, keywords in TREND_LANES.items():
                score = _trend_score(name, keywords)
                if score:
                    matches.append((score, lane))

            if not matches:
                continue

            # Assign a trend to its strongest lane only, avoiding noisy duplicates.
            score, lane = max(matches, key=lambda item: item[0])
            # One-word generic matches such as "code", "model", or "win" are too noisy
            # unless X's dedicated category gives us stronger context.
            if score == 1 and source_category not in ("sports",):
                continue

            filtered[lane].append(
                {"name": name, "score": score, "source": source_category}
            )
            seen.add(key)

    for lane in filtered:
        filtered[lane].sort(key=lambda item: item["score"], reverse=True)
        filtered[lane] = filtered[lane][:max_per_lane]

    return filtered


async def collect_x_trends(count=30):
    """Fetch live X trends from the authenticated session."""
    client = Client("en-US")
    client.set_cookies(
        {
            "auth_token": os.environ["X_AUTH_TOKEN"],
            "ct0": os.environ["X_CT0"],
        }
    )

    categories = ["trending", "for-you", "news", "sports"]
    result = {}

    for category in categories:
        try:
            trends = await client.get_trends(category, count=count, retry=False)
            names = []
            for trend in trends:
                name = getattr(trend, "name", None) or str(trend)
                name = str(name).strip()
                if name and name not in names:
                    names.append(name)
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
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not title:
                continue

            key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)

            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:800]
            published = entry.get("published", entry.get("updated", ""))

            candidates.append(
                {
                    "source": source,
                    "title": title,
                    "summary": summary,
                    "published": published,
                    "url": link,
                }
            )

    return candidates


def gemini_generate(candidates, history, filtered_trends):
    api_key = os.environ["GEMINI_API_KEY"]
    recent_history = history[-20:]

    prompt = f"""You write ONE short, funny, natural X post for a personal tech/developer account.

FILTERED LIVE X TRENDS RIGHT NOW:
{json.dumps(filtered_trends, ensure_ascii=False, indent=2)}

Fresh tech/developer candidate stories:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Recent posts already used:
{json.dumps(recent_history, ensure_ascii=False)}

CONTENT LANES — stay inside these:
1. AI / AI tools / AI engineering
2. Developers / programming / developer tools / IDEs
3. Vibe coding / AI-assisted coding
4. Sports — only when the live sports trend is genuinely interesting enough to make a sharp developer-style observation

TREND-FIRST RULE:
- The FILTERED LIVE X TRENDS are the primary signal for what is hot right now.
- Prefer a relevant live X trend over an ordinary RSS story.
- The trend must belong to one of the four lanes above.
- You may use the supplied RSS stories only to add factual context to a relevant trend.
- If the filtered X trends contain relevant options, choose the strongest current one.
- If there are no relevant filtered X trends, choose the strongest fresh tech/developer RSS story instead.
- Never force an unrelated trend into the account's niche.
- Never use a trend merely because it is popular; relevance comes first.

Choose the strongest CURRENT topic. Prioritize:
- genuinely fresh topics
- things developers are likely to care about
- surprise, irony, absurdity, or meme potential
- practical developer/tooling topics when timely
- a recognizable topic people are already discussing on X

Avoid:
- politics
- celebrity gossip
- generic world news
- crypto/finance
- medical/legal claims
- unrelated entertainment
- stale or low-signal stories
- duplicated stories
- religious or political hashtags unless the supplied trend is clearly a sports/AI/developer topic

Then write an original observation or joke. Do NOT rewrite the headline or simply announce the trend.

VOICE:
- sounds like a real developer casually posting
- dry humor, understated sarcasm, clever observation, or relatable dev humor
- punchy and specific
- confident but not corporate
- no forced meme language
- no fake enthusiasm

FACTUALITY / AUTHENTICITY:
- Only state facts supported by the supplied X trends or candidate stories.
- Never invent facts, numbers, prices, quotes, product capabilities, launches, or events.
- NEVER invent a personal experience, action, conversation, test, purchase, or opinion for the account owner.
- Do not use first-person claims such as "I", "I'm", "I've", "my", "me", or "mine".
- Do not invent dollar amounts, prices, counts, measurements, or other specific numbers.
- Do not claim to have used a product.
- Do not say "we" unless it clearly refers to developers/users generally.
- Do not mention Gemini, ChatGPT, or being an AI merely to explain how the post was generated.

IMPORTANT: If a joke requires inventing a scenario, personal anecdote, price, number, or specific detail that is not in the source material, abandon that joke and write a simpler observation based only on the supplied facts.

HARD POST RULES:
- Maximum {TARGET_POST_LENGTH} characters.
- Plain text only.
- No URL.
- No hashtags.
- No emojis unless genuinely necessary for the joke.
- No thread, title, list, bullets, or quote formatting.
- No engagement bait: no "Agree?", "Thoughts?", "Who else?", etc.
- No manufactured controversy.
- No marketing/corporate language.
- Avoid generic AI-sounding phrases including: {", ".join(CANNED_PHRASES)}.
- Do not mention that the post was generated.
- Avoid repetitive template openings.

Return ONLY valid JSON:
{{"topic":"...","reason":"...","post":"..."}}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    body = json.dumps(
        {"contents": [{"parts": [{"text": prompt}]}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

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
    if any(phrase in post.lower() for phrase in CANNED_PHRASES):
        raise ValueError("Canned/AI-sounding phrase detected")
    if re.search(r"\b(i|i'm|i’ve|i've|my|me|mine)\b", post.lower()):
        raise ValueError("First-person/personal claim detected")
    if re.search(r"(?:\$|€|£)\s*\d|\b\d+(?:\.\d+)?\s*(?:dollars|bucks|million|billion|thousand)\b", post.lower()):
        raise ValueError("Unverified number or price detected")
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
    directory = os.path.dirname(HISTORY_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-100:], f, ensure_ascii=False, indent=2)


async def post_to_x(text):
    client = Client("en-US")
    client.set_cookies(
        {
            "auth_token": os.environ["X_AUTH_TOKEN"],
            "ct0": os.environ["X_CT0"],
        }
    )
    user = await client.user()
    print(f"Authenticated as @{user.screen_name}")
    tweet = await client.create_tweet(text=text)
    return getattr(tweet, "id", "unknown")


def main():
    candidates = collect_candidates()
    if not candidates:
        raise RuntimeError("No feed candidates found")

    x_trends = asyncio.run(collect_x_trends())
    filtered_trends = filter_x_trends(x_trends)
    print("FILTERED X TRENDS:", json.dumps(filtered_trends, ensure_ascii=False, indent=2))

    if not any(filtered_trends.values()):
        print("No relevant X trends found; writer will use fresh RSS candidates.")

    history = load_history()
    last_error = None

    for attempt in range(3):
        try:
            result = gemini_generate(candidates, history, filtered_trends)
            validate(result["post"], history)
            break
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            if attempt == 2:
                raise RuntimeError(
                    f"Could not produce a valid post after 3 attempts: {exc}"
                ) from exc
    else:
        raise RuntimeError(f"Could not produce a valid post: {last_error}")

    print(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "attempt": attempt + 1,
                "character_count": len(result["post"]),
                **result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if os.environ.get("POST_TO_X", "false").lower() != "true":
        print("DRY RUN: set POST_TO_X=true only when you are ready to publish.")
        return

    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
