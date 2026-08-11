"""
Extract all available metadata + media URLs for an Instagram post/reel/carousel
using instaloader.

CLI:
    python main.py "https://www.instagram.com/p/XXXXXXXXX/"
    python main.py "https://www.instagram.com/reel/XXXXXXXXX/" --login your_username

API:
    flask --app main run
    POST /api/instagram {"url": "https://www.instagram.com/reel/XXXXXXXXX/"}
    GET  /api/instagram?url=https://www.instagram.com/reel/XXXXXXXXX/
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import instaloader
from flask import Flask, jsonify, request


app = Flask(__name__)


def extract_shortcode(url: str) -> str:
    """Pull the post/reel shortcode out of a full Instagram URL."""
    match = re.search(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Could not find a post/reel shortcode in URL: {url}")
    return match.group(1)


def create_instaloader() -> instaloader.Instaloader:
    return instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )


def load_cached_session(loader: instaloader.Instaloader, username: str, session_dir: Path | None = None) -> None:
    session_path = (session_dir or Path.cwd()) / f"session-{username}"
    loader.load_session_from_file(username, filename=str(session_path))


def interactive_login(loader: instaloader.Instaloader, username: str) -> None:
    session_file = Path(f"session-{username}")
    try:
        loader.load_session_from_file(username, filename=str(session_file))
        print(f"Loaded cached session for {username}")
    except FileNotFoundError:
        print(f"Logging in as {username} (you'll be prompted for a password)...")
        loader.interactive_login(username)
        loader.save_session_to_file(filename=str(session_file))
        print(f"Session cached to {session_file}")


def build_post_dict(post: instaloader.Post) -> dict:
    """
    Pull together the useful fields instaloader exposes on a Post object,
    including per-slide media URLs for carousels.
    """
    data = {
        "shortcode": post.shortcode,
        "post_id": post.mediaid,
        "type": post.typename,
        "is_video": post.is_video,
        "title": post.title,
        "caption": post.caption,
        "caption_hashtags": post.caption_hashtags,
        "caption_mentions": post.caption_mentions,
        "owner_username": post.owner_username,
        "owner_id": post.owner_id,
        "date_utc": post.date_utc.isoformat(),
        "likes": post.likes,
        "comments": post.comments,
        "video_view_count": post.video_view_count if post.is_video else None,
        "video_duration": post.video_duration if post.is_video else None,
        "location": str(post.location) if post.location else None,
        "is_sponsored": post.is_sponsored,
        "accessibility_caption": post.accessibility_caption,
        "url": post.url,
        "video_url": post.video_url if post.is_video else None,
        "slides": [],
    }

    if post.typename == "GraphSidecar":
        for index, node in enumerate(post.get_sidecar_nodes()):
            data["slides"].append(
                {
                    "index": index,
                    "is_video": node.is_video,
                    "display_url": node.display_url,
                    "video_url": node.video_url if node.is_video else None,
                }
            )
    else:
        data["slides"].append(
            {
                "index": 0,
                "is_video": post.is_video,
                "display_url": post.url,
                "video_url": post.video_url if post.is_video else None,
            }
        )

    return data


def fetch_instagram_metadata(url: str, login: str | None = None, allow_interactive_login: bool = False) -> dict:
    shortcode = extract_shortcode(url)
    loader = create_instaloader()

    username = login or os.getenv("INSTAGRAM_USERNAME")
    if username:
        if allow_interactive_login:
            interactive_login(loader, username)
        else:
            session_dir = Path(os.getenv("INSTAGRAM_SESSION_DIR", "."))
            load_cached_session(loader, username, session_dir=session_dir)

    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return build_post_dict(post)


@app.get("/")
def health() -> tuple[dict, int]:
    return {"ok": True, "service": "instagram-metadata-api"}, 200


@app.route("/api/instagram", methods=["GET", "POST"])
@app.route("/api/metadata", methods=["GET", "POST"])
def instagram_metadata_endpoint():
    payload = request.get_json(silent=True) or {}
    url = payload.get("url") or request.args.get("url")
    login = payload.get("login") or request.args.get("login")

    if not url:
        return jsonify({"error": "Missing required parameter: url"}), 400

    try:
        data = fetch_instagram_metadata(url, login=login, allow_interactive_login=False)
    except FileNotFoundError:
        return (
            jsonify(
                {
                    "error": "Instagram session file was not found for the requested login.",
                    "hint": "Call without login for public posts, or provide a cached session file during deployment.",
                }
            ),
            401,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return (
            jsonify(
                {
                    "error": "Failed to fetch Instagram metadata.",
                    "details": str(exc),
                    "hint": "Instagram may be rate-limiting anonymous requests. Try again with a cached session.",
                }
            ),
            502,
        )

    return jsonify(data), 200


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump full Instagram post/reel metadata to JSON via instaloader.")
    parser.add_argument("url", help="Instagram post/reel URL")
    parser.add_argument(
        "--login",
        help="Your Instagram username, to log in for private posts / to avoid rate limits.",
        default=None,
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output JSON file path (default: instagram_data.json in current dir).",
        default="instagram_data.json",
    )
    args = parser.parse_args()

    try:
        data = fetch_instagram_metadata(args.url, login=args.login, allow_interactive_login=True)
    except Exception as exc:
        print(f"ERROR: Failed to fetch post. {exc}")
        print("If this is a private post, or you're being rate-limited, try again with --login your_username")
        sys.exit(1)

    out_path = Path(args.output)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)

    print(f"\nSaved full metadata JSON to: {out_path.resolve()}")
    print("\n=== KEY METADATA ===")
    print(f"Type:         {data['type']} ({'video' if data['is_video'] else 'image/carousel'})")
    print(f"Owner:        {data['owner_username']}")
    print(f"Date:         {data['date_utc']}")
    print(f"Likes:        {data['likes']}")
    print(f"Comments:     {data['comments']}")
    print(f"Caption:      {(data['caption'] or '')[:200]}")

    print(f"\n=== SLIDES ({len(data['slides'])}) ===")
    for slide in data["slides"]:
        kind = "VIDEO" if slide["is_video"] else "IMAGE"
        media_url = slide["video_url"] if slide["is_video"] else slide["display_url"]
        print(f"  Slide {slide['index']}: {kind} -> {media_url}")


if __name__ == "__main__":
    main()
