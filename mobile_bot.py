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

    prompt = f"""You write ONE short, funny, natural X post for a personal tech/developer account.

Fresh candidate stories:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Recent posts already used:
{json.dumps(recent_history, ensure_ascii=False)}

Choose the strongest CURRENT tech topic. Prioritize:
- genuinely fresh stories
- things developers are likely to care about
- topics with surprise, irony, absurdity, or meme potential
- stories that appear prominent across sources
- practical developer/tooling topics when they are timely

Avoid:
- stale or low-signal stories
- duplicated stories
- political outrage bait
- medical/legal/financial claims unless the story is directly about a technology product or developer tool
- topics where the candidate text does not provide enough factual support

Then write an original observation or joke. Do NOT rewrite the headline.

VOICE:
- sounds like a real developer casually posting
- dry humor, understated sarcasm, clever observation, or relatable dev humor
- punchy and specific
- confident but not corporate
- no forced meme language
- no fake enthusiasm

FACTUALITY / AUTHENTICITY:
- Only state facts supported by the supplied candidate stories.
- Never invent facts, numbers, quotes, product capabilities, launches, or events.
- NEVER invent a personal experience, action, conversation, test, purchase, or opinion for the account owner.
- Avoid fake first-person setups such as "I asked...", "I tried...", "I spent...", "my..." unless that exact experience is explicitly present in the candidate data.
- Do not claim to have used a product.
- Do not say "we" unless it is clearly referring to developers/users generally.
- Do not mention Gemini, ChatGPT, or being an AI merely to explain how the post was generated.

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

    for attempt in range(3):
        try:
            result = gemini_generate(candidates, history)
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

    import asyncio

    tweet_id = asyncio.run(post_to_x(result["post"]))
    history.append(result["post"])
    save_history(history)
    print(f"POSTED TO X: {tweet_id}")


if __name__ == "__main__":
    main()
