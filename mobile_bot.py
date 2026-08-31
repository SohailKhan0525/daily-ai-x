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
]


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


def gemini_generate(candidates, history):
    api_key = os.environ["GEMINI_API_KEY"]
    recent_history = history[-20:]

    prompt = f"""You write one natural X post for a personal account about AI, software, developer tools, startups and technology.

Here are fresh candidate stories collected from multiple sources:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Recent posts already used:
{json.dumps(recent_history, ensure_ascii=False)}

Your job:
1. Choose the strongest CURRENT topic, not merely the first headline.
2. Prefer stories with freshness, broad developer relevance, surprise, practical usefulness, or strong meme/joke potential.
3. Give extra weight when a topic appears across multiple sources or is clearly prominent in the feeds.
4. Ignore stale, duplicated, promotional, or low-signal stories.
5. Write an original observation/joke about the topic. Do not rewrite the headline.

Style:
- Casual developer voice.
- Dry humor, playful sarcasm, clever understatement, or relatable developer humor.
- Short and punchy.
- It should sound like something a real person would casually post.
- Don't force a joke if the topic doesn't support one.

Hard rules:
- Maximum {TARGET_POST_LENGTH} characters (well below X's 280-character post limit).
- Plain text only.
- No URL, no hashtags, no emojis unless genuinely useful.
- No thread, title, list, or bullet points.
- No marketing/corporate tone.
- Never claim personal experience you do not have.
- Never invent facts, numbers, quotes, launches, or capabilities.
- Don't simply paraphrase the source headline.
- Don't use engagement bait such as "Agree?", "Thoughts?", or "Who else?".
- Don't manufacture controversy.
- Avoid generic AI-sounding/corporate phrases including: {", ".join(CANNED_PHRASES)}.
- Do not mention that you are an AI or that the post was generated.
- Avoid repetitive sentence patterns and obvious template openings.

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

    history = load_history()
    last_error = None

    # Retry a couple of times so one awkward Gemini response doesn't stop the day.
    for attempt in range(3):
        try:
            result = gemini_generate(candidates, history)
            validate(result["post"], history)
            break
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            last_error = exc
            if attempt == 2:
                raise RuntimeError(f"Could not produce a valid post after 3 attempts: {exc}") from exc
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

    import asyncio

    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
