import asyncio
import os

from twikit import Client


async def main():
    auth_token = os.environ.get("X_AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0")

    if not auth_token or not ct0:
        raise SystemExit(
            "Missing X_AUTH_TOKEN or X_CT0. Add them as GitHub Actions secrets before running this test."
        )

    client = Client("en-US", impersonate="chrome124")
    client.set_cookies({"auth_token": auth_token, "ct0": ct0})

    if not await client.is_logged_in():
        raise SystemExit("X authentication failed: cookies may be stale or invalid.")

    user = await client.user()
    print(f"X authentication OK for @{user.screen_name}")
    print("SAFE TEST ONLY: no post was created.")


if __name__ == "__main__":
    asyncio.run(main())
