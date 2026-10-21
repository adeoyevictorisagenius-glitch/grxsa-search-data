
import os
import json
import base64
import re
from pathlib import Path

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
import requests

load_dotenv()
# ── Configuration: all secrets come from environment variables ──

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ.get(
    "GITHUB_OWNER", "adeoyevictorisagenius-glitch"
)
GITHUB_REPO = os.environ.get("GITHUB_REPO", "grxsa-search-data")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
GITHUB_FILE_PATH = os.environ.get(
    "GITHUB_FILE_PATH", "search_suggestions.json"
)

GITHUB_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/"
    f"{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
)

# Set GOOGLE_APPLICATION_CREDENTIALS to your Firebase service
# account JSON file path before running this script.

MAX_SUGGESTION_LENGTH = 120
MAX_SUGGESTIONS = 2000
REQUEST_TIMEOUT = 30


def clean_text(value):
    """Normalize text and reject empty or unsuitable candidates."""
    if not isinstance(value, str):
        return ""

    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return ""

    if len(value) > MAX_SUGGESTION_LENGTH:
        value = value[:MAX_SUGGESTION_LENGTH].rsplit(" ", 1)[0]

    return value.strip()


def unique_suggestions(items):
    """Deduplicate without changing the original capitalization."""
    results = []
    seen = set()

    for item in items:
        item = clean_text(item)
        key = item.casefold()

        if not item or key in seen:
            continue

        seen.add(key)
        results.append(item)

    return results[:MAX_SUGGESTIONS]


def initialize_firestore():
    """Initialize Firebase locally or on GitHub Actions."""
    if not firebase_admin._apps:
        service_account_json = os.getenv(
            "FIREBASE_SERVICE_ACCOUNT_JSON"
        )

        if service_account_json:
            service_account = json.loads(service_account_json)
            cred = credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred)
        else:
            # Local development uses GOOGLE_APPLICATION_CREDENTIALS.
            firebase_admin.initialize_app()

    return firestore.client()



def extract_snippets(value, max_snippets=3):
    """Extract short, sentence-like snippets from text."""
    if not isinstance(value, str):
        return []

    # Split on sentence-ending punctuation or line breaks.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", value.strip())

    snippets = []

    for sentence in sentences:
        sentence = clean_text(sentence)

        # Skip fragments that are too short to be useful.
        if len(sentence) < 15:
            continue

        snippets.append(sentence)

        if len(snippets) >= max_snippets:
            break

    return snippets


def collect_firestore_suggestions(db):
    """Collect usernames, titles, and snippets from Firestore."""
    suggestions = []

    # Usernames and display names
    print("Reading Firestore collection: users")

    for document in db.collection("users").stream():
        data = document.to_dict() or {}

        username = clean_text(data.get("username"))
        display_name = clean_text(data.get("displayName"))

        if username:
            suggestions.append(username)

        if display_name:
            suggestions.append(display_name)

    # Videos and posts
    for collection_name in ("videos", "posts"):
        print(f"Reading Firestore collection: {collection_name}")

        for document in db.collection(collection_name).stream():
            data = document.to_dict() or {}

            # Prioritize titles as the most useful suggestions.
            title = clean_text(data.get("title"))

            if title:
                suggestions.append(title)

            # Collect useful snippets rather than only selecting
            # one field and ignoring the rest.
            fields = (
                ("description",)
                if collection_name == "videos"
                else ("text",)
            )

            for field in fields:
                snippets = extract_snippets(data.get(field))

                suggestions.extend(snippets)

    return unique_suggestions(suggestions)



def github_headers():
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_existing_file():
    """Fetch the current JSON and its SHA, needed for GitHub updates."""
    response = requests.get(
        GITHUB_API,
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 404:
        return {"version": 1, "suggestions": []}, None

    response.raise_for_status()
    file_data = response.json()

    if file_data.get("encoding") != "base64":
        raise ValueError("GitHub did not return a base64 file.")

    raw_content = base64.b64decode(
        file_data["content"]
    ).decode("utf-8")

    existing_data = json.loads(raw_content)

    if not isinstance(existing_data, dict):
        raise ValueError("The existing JSON root must be an object.")

    if not isinstance(existing_data.get("suggestions"), list):
        raise ValueError(
            "The existing JSON must contain a suggestions list."
        )

    return existing_data, file_data["sha"]


def commit_to_github(content, existing_sha):
    """Create or update the JSON file through the GitHub Contents API."""
    encoded_content = base64.b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    payload = {
        "message": "Update GRXSA search suggestions",
        "content": encoded_content,
        "branch": GITHUB_BRANCH,
    }

    if existing_sha:
        payload["sha"] = existing_sha

    response = requests.put(
        GITHUB_API,
        headers=github_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    print("Successfully committed updated suggestions to GitHub.")


def main():
    # Validate configuration before accessing external services.
    if not GITHUB_TOKEN.strip():
        raise ValueError("GITHUB_TOKEN cannot be empty.")

    db = initialize_firestore()
    firestore_suggestions = collect_firestore_suggestions(db)

    print(f"Collected {len(firestore_suggestions)} Firestore suggestions.")

    existing_data, existing_sha = fetch_existing_file()
    existing_suggestions = existing_data["suggestions"]

    # Preserve existing topics and append new real content.
    merged = unique_suggestions(
        existing_suggestions + firestore_suggestions
    )

    updated_data = {
        "version": existing_data.get("version", 1),
        "suggestions": merged,
    }

    new_content = json.dumps(
        updated_data,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    old_content = json.dumps(
        existing_data,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    if new_content == old_content:
        print("No changes detected. GitHub was not updated.")
        return

    commit_to_github(new_content, existing_sha)

    print(f"Total suggestions: {len(merged)}")
    print(
        f"Added {len(merged) - len(unique_suggestions(existing_suggestions))} "
        "new unique suggestions before applying the configured limit."
    )


if __name__ == "__main__":
    main()
