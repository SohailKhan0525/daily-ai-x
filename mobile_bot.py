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
    "game-changer", "game changer", "revolutionizing", "revolutionary",
    "unlock the power", "exciting times", "let that sink in", "the future is here",
    "in today's rapidly evolving", "in the rapidly evolving", "it's worth noting",
    "as we navigate", "this highlights the importance", "transformative",
    "seamlessly", "delve into",
]

# These are the only content lanes allowed into the writer.
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

# Claims that are very easy for a model to invent and that should not pass the gate
# unless they are explicitly present in the supplied source material.
UNSUPPORTED_CLAIM_PATTERNS = [
    r"\bmost effective\b", r"\bmost successful\b", r"\bmost popular\b",
    r"\bbest ever\b", r"\bworst ever\b", r"\bbiggest ever\b", r"\bfirst ever\b",
    r"\bin a decade\b", r"\bin years\b", r"\bfor years\b", r"\bever\b",
    r"\bthe biggest\b", r"\bthe best\b", r"\bthe worst\b", r"\bthe greatest\b",
    r"\bunprecedented\b", r"\bhistoric\b", r"\bmassive\b", r"\bhuge\b",
    r"\bclearly the\b", r"\bby far\b", r"\bnumber one\b", r"\bno one saw\b",
]


def _trend_score(name, keywords):
    text = name.casefold()
    return sum(2 if " " in keyword else 1 for keyword in keywords if keyword in text)


def filter_x_trends(x_trends, max_per_lane=8):
    filtered = {lane: [] for lane in TREND_LANES}
    seen = set()

    # X's dedicated sports feed gets priority for sports classification.
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

            score, lane = max(matches, key=lambda item: item[0])
            if score == 1 and source_category != "sports":
                continue

            filtered[lane].append({"name": name, "score": score, "source": source_category})
            seen.add(key)

    for lane in filtered:
        filtered[lane].sort(key=lambda item: item["score"], reverse=True)
        filtered[lane] = filtered[lane][:max_per_lane]
    return filtered


async def collect_x_trends(count=30):
    client = Client("en-US")
    client.set_cookies({
        "auth_token": os.environ["X_AUTH_TOKEN"],
        "ct0": os.environ["X_CT0"],
    })

    result = {}
    for category in ["trending", "for-you", "news", "sports"]:
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
            candidates.append({
                "source": source,
                "title": title,
                "summary": summary,
                "published": entry.get("published", entry.get("updated", "")),
                "url": link,
            })
    return candidates


def gemini_generate(candidates, history, filtered_trends):
    api_key = os.environ["GEMINI_API_KEY"]
    recent_history = history[-20:]
    prompt = f"""Write ONE short, funny, natural X post for a personal tech/developer account.

FILTERED LIVE X TRENDS:
{json.dumps(filtered_trends, ensure_ascii=False, indent=2)}

FRESH RSS CANDIDATES:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

RECENT POSTS:
{json.dumps(recent_history, ensure_ascii=False)}

FOUR ALLOWED LANES:
1. AI / AI tools / AI engineering
2. Developers / programming / developer tools / IDEs
3. Vibe coding / AI-assisted coding
4. Sports — only when a live sports trend is genuinely interesting enough for a sharp developer-style observation

SOURCE PRIORITY:
- X trends are the primary signal for what is hot now.
- If a relevant filtered X trend exists, choose one of those exact trend names as the topic.
- RSS may only add factual context to that topic.
- If no relevant X trend exists, use the strongest fresh RSS candidate.
- Never force an unrelated popular trend into the account's niche.

FACTUALITY IS A HARD CONSTRAINT:
- Every factual claim must be directly supported by the supplied trend or RSS material.
- Do not infer motives, rankings, historical significance, market impact, popularity, effectiveness, or outcomes unless the source explicitly says so.
- Never invent numbers, prices, dates, quotes, product capabilities, launches, comparisons, anecdotes, tests, purchases, or personal experiences.
- NEVER turn a weak fact into a strong claim. For example, do not turn "Google is removing Manifest V2" into claims about the "most effective marketing campaign" or "the biggest change in a decade".
- Avoid unsupported superlatives and historical claims such as "best", "worst", "biggest", "most effective", "unprecedented", "in a decade", "ever", or "by far" unless those exact claims are explicitly supported by the supplied material.
- If a joke needs an invented detail, abandon that joke and write a simpler observation using only supported facts.

VOICE:
- dry humor, understated sarcasm, clever observation, relatable dev humor
- punchy, specific, casual, not corporate
- do not merely rewrite the headline

AVOID:
- politics, celebrity gossip, generic world news, crypto/finance, medical/legal claims
- unrelated entertainment, stale stories, duplicated stories
- marketing/corporate language
- engagement bait such as "Agree?", "Thoughts?", or "Who else?"
- first-person/personal claims: I, I'm, I've, my, me, mine
- mentioning Gemini, ChatGPT, or AI as the generator

HARD POST RULES:
- maximum {TARGET_POST_LENGTH} characters
- plain text only
- no URLs, hashtags, threads, bullets, or quote formatting
- no fake enthusiasm
- avoid these phrases: {", ".join(CANNED_PHRASES)}

Return ONLY valid JSON:
{{"topic":"...","reason":"...","post":"..."}}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
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


def _evidence_terms(filtered_trends, candidates):
    evidence = []
    for lane_items in filtered_trends.values():
        evidence.extend(item["name"] for item in lane_items)
    evidence.extend(item["title"] for item in candidates)
    evidence.extend(item["summary"] for item in candidates)
    return " ".join(evidence).casefold()


def _meaningful_words(text):
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
        "from", "after", "before", "is", "are", "was", "were", "this", "that",
        "it", "its", "has", "have", "had", "as", "at", "by", "be", "been",
        "into", "than", "their", "they", "them", "will", "can", "just", "now",
    }
    words = re.findall(r"[a-z0-9][a-z0-9'’-]*", text.casefold())
    return {word for word in words if len(word) >= 4 and word not in stop}


def validate(post, history, filtered_trends, candidates):
    if not post:
        raise ValueError("Empty post")
    if len(post) > MAX_POST_LENGTH:
        raise ValueError(f"Post is {len(post)} characters; X limit is {MAX_POST_LENGTH}")
    if re.search(r"https?://|www\\.", post, re.I):
        raise ValueError("URL detected")
    if any(phrase in post.lower() for phrase in CANNED_PHRASES):
        raise ValueError("Canned/AI-sounding phrase detected")
    if re.search(r"\b(i|i'm|i’ve|i've|my|me|mine)\b", post.lower()):
        raise ValueError("First-person/personal claim detected")
    if re.search(r"(?:\$|€|£)\s*\d|\b\d+(?:\.\d+)?\s*(?:dollars|bucks|million|billion|thousand)\b", post.lower()):
        raise ValueError("Unverified number or price detected")
    for pattern in UNSUPPORTED_CLAIM_PATTERNS:
        if re.search(pattern, post, re.I):
            raise ValueError("Unsupported comparative/historical claim detected")
    if post in history:
        raise ValueError("Duplicate post")

    # Require the generated post to retain at least one meaningful source term.
    # This is a safety net against a totally unrelated model invention.
    post_words = _meaningful_words(post)
    evidence_words = _meaningful_words(_evidence_terms(filtered_trends, candidates))
    if post_words and not (post_words & evidence_words):
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

    x_trends = asyncio.run(collect_x_trends())
    filtered_trends = filter_x_trends(x_trends)
    print("FILTERED X TRENDS:", json.dumps(filtered_trends, ensure_ascii=False, indent=2))

    history = load_history()
    last_error = None
    for attempt in range(3):
        try:
            result = gemini_generate(candidates, history, filtered_trends)
            validate(result["post"], history, filtered_trends, candidates)
            break
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            print(f"WRITER REJECTED ATTEMPT {attempt + 1}: {exc}")
            if attempt == 2:
                raise RuntimeError(f"Could not produce a valid post after 3 attempts: {exc}") from exc

    print(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "attempt": attempt + 1,
        "character_count": len(result["post"]),
        **result,
    }, ensure_ascii=False, indent=2))

    if os.environ.get("POST_TO_X", "false").lower() != "true":
        print("DRY RUN: set POST_TO_X=true only when you are ready to publish.")
        return

    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
