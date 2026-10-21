
import os
import json
import base64
import re
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
import requests


load_dotenv()

# --------------------------------------------------
# Configuration
# --------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_OWNER = os.getenv(
    "GITHUB_OWNER", "adeoyevictorisagenius-glitch"
)
GITHUB_REPO = os.getenv("GITHUB_REPO", "grxsa-search-data")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_FILE_PATH = os.getenv(
    "GITHUB_FILE_PATH", "search_suggestions.json"
)

FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_JSON"
)

GITHUB_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/"
    f"{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
)

MAX_SUGGESTION_LENGTH = 120
MAX_SUGGESTIONS = 2000
REQUEST_TIMEOUT = 30

# These are permanent, curated topics.
# Add any hand-picked topics you want to preserve here.
CURATED_SUGGESTIONS = [
    "Physics",
    "Quantum physics",
    "Classical mechanics",
    "Electromagnetism",
    "Thermodynamics",
    "Space exploration",
    "Astronomy",
    "Black holes",
    "Relativity",
    "Rocket science",
    "Robotics",
    "Artificial intelligence",
    "Machine learning",
    "Python programming",
    "Flutter development",
    "Computer science",
    "Electronics",
    "Arduino projects",
    "Engineering",
    "Renewable energy",
    "Climate science",
    "Mathematics",
    "Calculus",
    "Algebra",
    "Biology",
    "Genetics",
    "Chemistry",
    "Scientific experiments",
    "Inventions",
    "Technology projects",
]


# --------------------------------------------------
# Text helpers
# --------------------------------------------------

def clean_text(value):
    """Normalize a string without changing its meaning."""
    if not isinstance(value, str):
        return ""

    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return ""

    return value[:MAX_SUGGESTION_LENGTH]


def unique_suggestions(items):
    """Deduplicate suggestions while preserving their order."""
    result = []
    seen = set()

    for item in items:
        text = clean_text(item)

        if not text:
            continue

        key = text.casefold()

        if key in seen:
            continue

        seen.add(key)
        result.append(text)

        if len(result) >= MAX_SUGGESTIONS:
            break

    return result


def extract_snippets(value, max_snippets=3):
    """Extract useful sentences from a description."""
    if not isinstance(value, str):
        return []

    value = value.strip()

    if not value:
        return []

    sentences = re.split(r"(?<=[.!?])\s+|\n+", value)
    snippets = []

    for sentence in sentences:
        sentence = clean_text(sentence)

        # Avoid adding tiny fragments as suggestions.
        if len(sentence) < 15:
            continue

        snippets.append(sentence)

        if len(snippets) >= max_snippets:
            break

    return snippets


def document_is_deleted(data):
    """Ignore documents explicitly marked as deleted."""
    if data.get("isDeleted") is True:
        return True

    if data.get("deleted") is True:
        return True

    if data.get("markedForDeletion") is True:
        return True

    status = str(data.get("status", "")).strip().lower()

    return status in {"deleted", "removed"}


# --------------------------------------------------
# Firebase
# --------------------------------------------------

def initialize_firestore():
    """Initialize Firebase locally or in GitHub Actions."""
    if not firebase_admin._apps:
        if FIREBASE_SERVICE_ACCOUNT_JSON:
            service_account = json.loads(
                FIREBASE_SERVICE_ACCOUNT_JSON
            )

            cred = credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred)

        else:
            # Locally, configure GOOGLE_APPLICATION_CREDENTIALS
            # to point to your Firebase service-account JSON file.
            firebase_admin.initialize_app()

    return firestore.client()


def collect_firestore_suggestions(db):
    """
    Rebuild suggestions from CURRENT Firestore documents.

    Deleted documents are no longer returned by Firestore.
    Explicitly deleted/removed documents are also skipped.
    """
    suggestions = []

    # User suggestions
    for snapshot in db.collection("users").stream():
        data = snapshot.to_dict() or {}

        if document_is_deleted(data):
            continue

        for field in ("username", "displayName"):
            value = clean_text(data.get(field))

            if value:
                suggestions.append(value)

    # Video suggestions
    for snapshot in db.collection("videos").stream():
        data = snapshot.to_dict() or {}

        if document_is_deleted(data):
            continue

        title = clean_text(data.get("title"))

        if title:
            suggestions.append(title)

        suggestions.extend(
            extract_snippets(data.get("description"))
        )

    # Text-post suggestions
    for snapshot in db.collection("posts").stream():
        data = snapshot.to_dict() or {}

        if document_is_deleted(data):
            continue

        title = clean_text(data.get("title"))

        if title:
            suggestions.append(title)

        # Support the text fields used by different post versions.
        for field in ("text", "description"):
            suggestions.extend(
                extract_snippets(data.get(field))
            )

    return unique_suggestions(suggestions)


# --------------------------------------------------
# GitHub API
# --------------------------------------------------

def github_headers():
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN is missing. Configure it as a "
            "GitHub Actions secret/environment variable or in .env."
        )

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_existing_file():
    """Return the existing JSON data and its GitHub SHA."""
    response = requests.get(
        GITHUB_API,
        headers=github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 404:
        return {}, None

    response.raise_for_status()
    file_data = response.json()

    encoded_content = file_data.get("content", "")
    decoded_content = base64.b64decode(
        encoded_content
    ).decode("utf-8")

    existing_data = json.loads(decoded_content)

    if not isinstance(existing_data, dict):
        raise ValueError(
            "The existing suggestions file must contain a JSON object."
        )

    return existing_data, file_data.get("sha")


def commit_to_github(content, existing_sha):
    """Create or update the JSON file on GitHub."""
    encoded_content = base64.b64encode(
        content.encode("utf-8")
    ).decode("ascii")

    payload = {
        "message": "Refresh GRXSA search suggestions from live content",
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

    print("GitHub suggestions file updated successfully.")


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    print("Connecting to Firestore...")

    db = initialize_firestore()

    print("Reading current users, videos and posts...")

    live_suggestions = collect_firestore_suggestions(db)

    print(
        f"Collected {len(live_suggestions)} unique "
        "suggestions from current Firestore content."
    )

    print("Reading existing GitHub suggestions...")

    existing_data, existing_sha = fetch_existing_file()

    # Prefer the dedicated curated list after migration.
    # On the first run, use the built-in curated list so
    # old dynamic suggestions do not become permanent.
    saved_curated = existing_data.get("curated_suggestions")

    if isinstance(saved_curated, list):
        curated = unique_suggestions(saved_curated)
    else:
        curated = unique_suggestions(CURATED_SUGGESTIONS)

    # Rebuild from scratch. Do NOT merge old dynamic suggestions.
    final_suggestions = unique_suggestions(
        curated + live_suggestions
    )

    updated_data = {
        "version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "curated_suggestions": curated,
        "suggestions": final_suggestions,
    }

    new_content = json.dumps(
        updated_data,
        ensure_ascii=False,
        indent=2,
    ) + "\n"

    # Compare actual suggestion data, not timestamps or JSON formatting.
    old_suggestions = existing_data.get("suggestions", [])
    old_curated = existing_data.get("curated_suggestions")

    if (
        old_suggestions == final_suggestions
        and old_curated == curated
        and existing_data.get("version") == 2
    ):
        print("No changes detected. Skipping GitHub commit.")
        return

    print(
        f"Curated topics: {len(curated)}\n"
        f"Live suggestions: {len(live_suggestions)}\n"
        f"Total suggestions: {len(final_suggestions)}"
    )

    commit_to_github(new_content, existing_sha)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}")
        raise
