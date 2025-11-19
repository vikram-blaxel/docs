import os
import sys
import re
import json
import textwrap
from pathlib import Path
from typing import Any
from datetime import datetime
import subprocess

import requests

NOTION_API_URL = "https://api.notion.com/v1/pages"
NOTION_BLOCKS_API_URL = "https://api.notion.com/v1/blocks"
NOTION_VERSION = "2025-09-03"  # adjust if you want a newer version
DOCS_BASE_URL = "https://docs.blaxel.ai"

DOCS_JSON_NAME = "docs.json"
IGNORED_DIR_NAMES = {"node_modules", "img", "imgs", "images", "scripts"}


# ---------- Notion ID helpers ----------

def normalize_notion_id(raw: str) -> str:
    """
    Accepts a Notion URL, slug+id, or raw id and returns a dashed UUID.

    Examples it accepts:
      - "https://www.notion.so/My-Page-2aa22baf61cd80f3a075f5deaeb1cf6f?pvs=4"
      - "My-Page-2aa22baf61cd80f3a075f5deaeb1cf6f"
      - "2aa22baf61cd80f3a075f5deaeb1cf6f"
      - "2aa22baf-61cd-80f3-a075-f5deaeb1cf6f"
    """
    raw = raw.strip()

    # If it's a full URL, grab the last path segment before query params
    if raw.startswith("http://") or raw.startswith("https://"):
        raw = raw.split("?")[0].rstrip("/")
        raw = raw.split("/")[-1]

    # Remove dashes to make it easier to match
    cleaned = raw.replace("-", "")

    # Find a 32-hex-char sequence
    m = re.search(r"([0-9a-fA-F]{32})", cleaned)
    if not m:
        raise ValueError(
            f"Parent ID '{raw}' does not contain a valid 32-char Notion ID. "
            f"Make sure you copied the full ID from the Notion URL."
        )

    hex32 = m.group(1).lower()

    # Insert dashes as UUID
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"


# ---------- Text splitting helpers ----------

def split_for_rich_text(text: str, max_len: int = 1900, max_segments: int = 1000):
    """
    Split text into segments small enough for Notion rich_text items.

    Notion enforces a ~2000-char limit per rich_text element; we stay a bit
    under (1900) to be safe. max_segments is a safety guard.
    """
    segments = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + max_len, length)
        segments.append(text[start:end])
        start = end

        if len(segments) > max_segments:
            raise ValueError(
                f"Text is too long to fit into a single block even with "
                f"{max_segments} rich_text segments (~{max_len * max_segments} chars)."
            )

    return segments


def make_rich_text_items(text: str):
    """
    Helper to build a list of Notion rich_text items from a (possibly long) string.
    """
    return [
        {
            "type": "text",
            "text": {"content": segment},
        }
        for segment in split_for_rich_text(text)
    ]


# ---------- Frontmatter metadata parsing (title only, no stripping) ----------

def extract_title_from_frontmatter(md_content: str):
    """
    Look for a YAML-style frontmatter block at the very top:

    ---
    title: My Title
    other: value
    ---

    Returns the title string if found, otherwise None.
    Does NOT strip or modify the markdown; caller should still use the full content.
    """
    if not md_content.startswith("---"):
        return None

    lines = md_content.splitlines()
    if len(lines) < 3:
        return None

    # Find closing '---'
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    meta_lines = lines[1:end_idx]
    title_re = re.compile(r'^title:\s*["\']?(.*?)["\']?\s*$')

    for line in meta_lines:
        m = title_re.match(line.strip())
        if m:
            return m.group(1).strip()

    return None


# ---------- Notion blocks ----------

def build_markdown_code_block(md_content: str):
    """
    Build ONE Notion code block (language=markdown) that contains the entire
    markdown content, internally chunked into multiple rich_text segments.
    """
    return {
        "object": "block",
        "type": "code",
        "code": {
            "language": "markdown",
            "rich_text": make_rich_text_items(md_content),
        },
    }


def build_source_link_block(url: str):
    """
    Build a paragraph block with a link to the original docs page.
    """
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": "Source: ", "link": None},
                },
                {
                    "type": "text",
                    "text": {"content": url, "link": {"url": url}},
                },
            ]
        },
    }


# ---------- Page clearing helper ----------

def clear_page_children(parent_page_id: str, notion_token: str):
    """
    Remove all existing blocks (including child pages) from the given page.

    This archives every child block or child_page under the parent, effectively
    resetting the page before rebuilding the hierarchy.
    """
    parent_uuid = normalize_notion_id(parent_page_id)

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    print(f"[clear] Fetching children of page {parent_uuid}...")

    next_cursor = None
    total_archived = 0

    while True:
        params = {"page_size": 100}
        if next_cursor:
            params["start_cursor"] = next_cursor

        resp = requests.get(
            f"{NOTION_BLOCKS_API_URL}/{parent_uuid}/children",
            headers=headers,
            params=params,
        )

        if not resp.ok:
            raise RuntimeError(
                f"Failed to list children for page {parent_uuid}: "
                f"{resp.status_code} {resp.text}"
            )

        data = resp.json()
        results = data.get("results", [])

        print(f"[clear]   Retrieved {len(results)} children")

        if not results and not data.get("has_more"):
            break

        for block in results:
            block_id = block.get("id")
            obj_type = block.get("object")       # "block" or "page"
            block_type = block.get("type")       # "paragraph", "child_page", etc.

            if not block_id:
                print("[clear]   Warning: found child without ID, skipping")
                continue

            # Determine correct endpoint
            if obj_type == "page" or block_type == "child_page":
                endpoint = f"{NOTION_API_URL}/{block_id}"
                endpoint_type = "page"
            else:
                endpoint = f"{NOTION_BLOCKS_API_URL}/{block_id}"
                endpoint_type = "block"

            print(
                f"[clear]   Archiving {endpoint_type:<5} "
                f"id={block_id} (object={obj_type}, type={block_type})"
            )

            patch_resp = requests.patch(
                endpoint,
                headers=headers,
                json={"archived": True},
            )

            if not patch_resp.ok:
                print(
                    f"[clear]     ❌ Failed: {patch_resp.status_code} {patch_resp.text}"
                )
            else:
                print(f"[clear]     ✔ Archived successfully")
                total_archived += 1

        if not data.get("has_more"):
            break

        next_cursor = data.get("next_cursor")
        if next_cursor:
            print("[clear]   Fetching next page of children...")

    print(f"[clear] Done. Archived {total_archived} child blocks/pages under {parent_uuid}.")


# ---------- Generic Notion page creation ----------

def create_simple_notion_page(title: str, parent_page_id: str, notion_token: str):
    """
    Create a simple Notion page (used for structural / section nodes).
    """
    parent_uuid = normalize_notion_id(parent_page_id)

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    payload = {
        "parent": {"page_id": parent_uuid},
        "properties": {
            "title": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title},
                    }
                ]
            }
        },
        "children": [],
    }

    response = requests.post(NOTION_API_URL, headers=headers, json=payload)

    if not response.ok:
        raise RuntimeError(
            f"Failed to create Notion directory/section page: {response.status_code} {response.text}"
        )

    return response.json()


# ---------- Main page creation helper for markdown files ----------

def create_notion_page_from_markdown(
    markdown_path: str | Path,
    parent_page_id: str,
    notion_token: str,
    docs_url: str | None = None,
):
    markdown_path = Path(markdown_path)
    if not markdown_path.is_file():
        raise FileNotFoundError(f"Markdown file not found: {markdown_path}")

    with markdown_path.open("r", encoding="utf-8") as f:
        md_content = f.read()

    # Extract title from frontmatter (but keep content unchanged)
    meta_title = extract_title_from_frontmatter(md_content)

    # Fallback title if no metadata title found
    title = meta_title or markdown_path.stem

    # Normalize and validate parent id (URL, slug, or raw ID)
    parent_uuid = normalize_notion_id(parent_page_id)

    # Build children: source link block (if any) + single markdown code block
    children = []
    if docs_url:
        children.append(build_source_link_block(docs_url))
    children.append(build_markdown_code_block(md_content))

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    payload = {
        "parent": {"page_id": parent_uuid},
        "properties": {
            "title": {
                "title": [
                    {
                        "type": "text",
                        "text": {"content": title},
                    }
                ]
            }
        },
        "children": children,
    }

    response = requests.post(NOTION_API_URL, headers=headers, json=payload)

    if not response.ok:
        raise RuntimeError(
            f"Failed to create Notion page: {response.status_code} {response.text}"
        )

    return response.json()


# ---------- docs.json helpers (Mintlify-optimized) ----------

def load_docs_structure(root_dir: Path) -> dict:
    """
    Load docs.json from the given root directory and return the parsed dict.
    """
    docs_json_path = root_dir / DOCS_JSON_NAME
    if not docs_json_path.is_file():
        raise FileNotFoundError(
            f"docs.json not found in {root_dir}. Expected at: {docs_json_path}"
        )

    with docs_json_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_page_to_file(root_dir: Path, slug: str) -> Path | None:
    """
    Given a Mintlify page slug like:
      - "Overview"
      - "Sandboxes/Overview"
      - "api-reference/introduction"

    Resolve it to an actual markdown file under root_dir:
      - <root>/Overview.mdx or .md
      - <root>/Sandboxes/Overview.mdx or .md
      - <root>/api-reference/introduction.mdx or .md

    Rules:
      - Ignore if the *root-level* directory is in IGNORED_DIR_NAMES.
      - Prefer .mdx over .md.
      - If slug already has an extension and the file exists, accept it
        (but still enforce .md/.mdx).
    """
    rel = Path(slug)

    # Ignore paths whose first component is in IGNORED_DIR_NAMES
    if rel.parts and rel.parts[0] in IGNORED_DIR_NAMES:
        print(f"[skip] Slug under ignored root dir '{rel.parts[0]}': {slug}")
        return None

    # Case 1: slug has no extension -> try .mdx, then .md
    if rel.suffix == "":
        for ext in (".mdx", ".md"):
            candidate = (root_dir / (str(rel) + ext)).resolve()
            if candidate.is_file():
                return candidate

    # Case 2: slug might already include extension
    candidate = (root_dir / rel).resolve()
    if candidate.is_file() and candidate.suffix.lower() in (".md", ".mdx"):
        return candidate

    print(f"[warn] Could not resolve slug '{slug}' to a markdown file under {root_dir}")
    return None


def build_docs_url(root_dir: Path, file_path: Path) -> str:
    """
    Build the docs URL for a given markdown file, dropping the extension.

    E.g.
      root_dir = /repo/docs
      file_path = /repo/docs/Sandboxes/Overview.mdx
      -> https://docs.blaxel.ai/Sandboxes/Overview
    """
    rel_path = file_path.relative_to(root_dir)
    rel_no_ext = rel_path.with_suffix("")  # drop .md / .mdx
    rel_str = rel_no_ext.as_posix()
    return DOCS_BASE_URL.rstrip("/") + "/" + rel_str.lstrip("/")


def build_nav_nodes(docs_json: dict, root_dir: Path) -> list[dict]:
    """
    Convert Mintlify-style docs.json to a simple navigation tree:

      node = {
        "title": str,
        "file": Optional[Path],
        "children": list[node],
        "kind": "tab" | "group" | "section" | "page",
      }

    Behaviour:
      - Each tab with `groups` becomes a 'tab' structural node.
      - Each group becomes a 'group' structural node.
      - Each string in `pages` becomes a 'page' node (md/mdx).
      - Nested groups inside pages become 'group' nodes.
      - Only files that resolve to .md/.mdx under root_dir are included.
      - Paths starting under ignored root-level dirs (node_modules, img, etc.)
        are skipped.
    """

    nav = docs_json.get("navigation", {})
    tabs = nav.get("tabs", [])

    def build_group_node(group_obj: dict) -> dict | None:
        group_title = group_obj.get("group") or "Untitled group"
        pages = group_obj.get("pages", [])

        children: list[dict] = []

        for entry in pages:
            # Nested group
            if isinstance(entry, dict) and "group" in entry:
                nested = build_group_node(entry)
                if nested:
                    children.append(nested)
                continue

            # Plain page slug
            if isinstance(entry, str):
                file_path = resolve_page_to_file(root_dir, entry)
                if not file_path:
                    continue

                children.append(
                    {
                        "title": Path(entry).name,
                        "file": file_path,
                        "children": [],
                        "kind": "page",
                    }
                )
                continue

            # Anything else is unexpected; skip
            print(f"[skip] Unrecognized pages entry in group '{group_title}': {entry!r}")

        if not children:
            print(f"[info] Group '{group_title}' has no usable children, skipping.")
            return None

        return {
            "title": group_title,
            "file": None,
            "children": children,
            "kind": "group",
        }

    nodes: list[dict] = []

    for tab in tabs:
        tab_title = tab.get("tab") or "Untitled tab"
        groups = tab.get("groups")

        # Tabs like "Sandbox API Reference" may only have openapi and no groups -> skip.
        if not groups:
            continue

        tab_children: list[dict] = []

        for group_obj in groups:
            group_node = build_group_node(group_obj)
            if group_node:
                tab_children.append(group_node)

        if not tab_children:
            print(f"[info] Tab '{tab_title}' has no usable groups, skipping.")
            continue

        nodes.append(
            {
                "title": tab_title,
                "file": None,
                "children": tab_children,
                "kind": "tab",
            }
        )

    return nodes


# ---------- Export using docs.json structure ----------

def process_nav_nodes(
    nodes: list[dict],
    root_dir: Path,
    parent_page_id: str,
    notion_token: str,
    counters: dict,
    current_group: str | None = None,
):
    """
    Recursively create Notion pages according to the docs.json-derived structure.

    Rules:
      - Node with `file` -> create a content page from that markdown file.
      - Node without `file` but with children -> create a structural Notion page.
      - Node with both `file` and children -> children are nested under the file page.

    Counters:
      - counters["total_pages"]: total number of content pages (file-backed) created.
      - counters["groups"][group_title]: pages created under that Mintlify group.
    """
    for node in nodes:
        title = node["title"]
        file_path: Path | None = node.get("file")
        children = node.get("children", [])
        kind = node.get("kind")  # "tab" | "group" | "section" | "page"

        current_parent_id = parent_page_id

        # If this is a group node, update the current_group context
        next_group = current_group
        if kind == "group":
            next_group = title  # pages under here will count towards this group

        if file_path is None and children:
            # Structural node (tab/group/section)
            try:
                page = create_simple_notion_page(title, parent_page_id, notion_token)
            except Exception as e:
                print(f"Error creating structural page '{title}': {e}")
                continue

            page_id = page.get("id")
            if not page_id:
                print(f"Warning: structural page for '{title}' has no 'id' in response.")
                continue

            current_parent_id = page_id
            print(f"[section] Created Notion page for section '{title}' (kind={kind})")

            process_nav_nodes(
                children,
                root_dir,
                current_parent_id,
                notion_token,
                counters,
                current_group=next_group,
            )
            continue

        if file_path is not None:
            if not file_path.is_file():
                print(f"[warn] File listed in docs.json not found: {file_path}")
                continue

            try:
                docs_url = build_docs_url(root_dir, file_path)
                page = create_notion_page_from_markdown(
                    file_path,
                    current_parent_id,
                    notion_token,
                    docs_url=docs_url,
                )
            except Exception as e:
                print(f"Error creating page for file '{file_path}': {e}")
                continue

            page_id = page.get("id")
            notion_url = page.get("url", "(no url in response)")
            rel_display = file_path.relative_to(root_dir).as_posix()
            print(f"[file] Created Notion page for '{rel_display}': {notion_url}")

            # Update counters
            counters["total_pages"] += 1
            if current_group is not None:
                counters["groups"][current_group] = counters["groups"].get(current_group, 0) + 1

            # If this node also has children, nest them under the newly created page
            if children:
                if not page_id:
                    print(
                        f"Warning: cannot attach children under '{title}' "
                        f"because created page has no 'id'."
                    )
                else:
                    process_nav_nodes(
                        children,
                        root_dir,
                        page_id,
                        notion_token,
                        counters,
                        current_group=next_group,
                    )


def process_directory_with_docs_json(
    root_dir: Path,
    parent_page_id: str,
    notion_token: str,
):
    """
    Use Mintlify docs.json to drive the export.

    Only files listed in navigation.tabs[*].groups[*].pages are considered.
    Also accumulates statistics about pages created per group and in total.
    """
    root_dir = root_dir.resolve()

    docs_json = load_docs_structure(root_dir)
    nav_nodes = build_nav_nodes(docs_json, root_dir)

    if not nav_nodes:
        print(f"No usable navigation entries found in {DOCS_JSON_NAME} under {root_dir}")
        return {"total_pages": 0, "groups": {}}

    counters = {"total_pages": 0, "groups": {}}

    process_nav_nodes(
        nav_nodes,
        root_dir,
        parent_page_id,
        notion_token,
        counters,
        current_group=None,
    )

    # Log stats
    print("\n[stats] Pages created per group:")
    if not counters["groups"]:
        print("[stats]   (no group-scoped pages)")
    else:
        for group_name, count in sorted(counters["groups"].items()):
            print(f"[stats]   {group_name}: {count} page(s)")

    print(f"[stats] Total content pages created: {counters['total_pages']}")

    return counters

from datetime import datetime
import subprocess

def add_root_update_block(parent_page_id: str, notion_token: str):
    """
    Insert a paragraph block at the top of the root page showing
    the date/time of the import and the git commit ID.
    """
    parent_uuid = normalize_notion_id(parent_page_id)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Try to get the commit SHA
    try:
        commit_id = (
            subprocess.check_output(["git", "rev-parse", "HEAD"])
            .decode("utf-8")
            .strip()
        )
    except Exception:
        commit_id = "(unknown)"

    lines = [
        f"Last updated: {now}",
        f"Commit: {commit_id}",
    ]
    block_text = "\n".join(lines)

    block = {
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": block_text}
                        }
                    ]
                }
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    resp = requests.patch(
        f"{NOTION_BLOCKS_API_URL}/{parent_uuid}/children",
        headers=headers,
        json=block,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Failed to insert root update block: {resp.status_code} {resp.text}"
        )

    print(f"[root] Added 'Last updated' block to root page ({now}, commit {commit_id})")


# ---------- CLI ----------

def main():
    if len(sys.argv) < 2:
        print(
            textwrap.dedent(
                f"""
                Usage:
                  python import.py

                Args:
                  markdown_path          Path to a directory (with {DOCS_JSON_NAME}) or single .md/.mdx file

                Behaviour:
                  - The script ALWAYS wipes (archives) all existing content under
                    the given parent page before importing.

                  - If markdown_path is a directory:
                      * Expects a {DOCS_JSON_NAME} file in that directory using
                        Mintlify's schema.
                      * Reads the navigation structure from navigation.tabs[*].groups[*].pages.
                      * Builds a simplified structure that includes ONLY the files
                        referenced there.
                      * Ignores entries whose slugs start with ignored root dirs:
                          {", ".join(sorted(IGNORED_DIR_NAMES))}
                      * For each referenced page slug:
                          - Resolves to <root>/<slug>.mdx or .md (preferring .mdx)
                            or uses explicit extensions if present.
                      * Creates a Notion hierarchy matching the docs.json structure:
                          - Tabs -> top-level sections
                          - Groups -> nested sections
                          - Page strings -> content pages
                          - Nested groups inside pages -> deeper sections
                      * Each file page gets:
                          - A paragraph block at the top with a link
                            "{DOCS_BASE_URL}/<relative-path-without-extension>"
                          - A single markdown code block with the entire file contents.
                      * At the end, logs:
                          - Count of pages created per Mintlify group
                          - Total pages created

                  - If markdown_path is a single file:
                      * Clears all children of the parent page.
                      * Creates one Notion page under the given parent.
                      * The source link is:
                            "{DOCS_BASE_URL}/<file-name-without-extension>"
                      * Logs total pages created: 1

                  - For each markdown file:
                      * Uses the `title` field from frontmatter (if present)
                        as the Notion page title; otherwise falls back to
                        the file stem.

                Environment:
                  NOTION_TOKEN must be set to your Notion integration token.
                  NOTION_ROOT_PAGE must be set to your Notion root page ID.
                """
            ).strip()
        )
        sys.exit(1)

    markdown_path = Path(sys.argv[1])

    notion_token = os.environ.get("NOTION_TOKEN")
    if not notion_token:
        print("Error: NOTION_TOKEN environment variable is not set.")
        sys.exit(1)

    parent_page_id = os.environ.get("NOTION_ROOT_PAGE")
    if not parent_page_id:
        print("Error: NOTION_ROOT_PAGE environment variable is not set.")
        sys.exit(1)

    # Always clear existing content under the starting page
    try:
        clear_page_children(parent_page_id, notion_token)
    except Exception as e:
        print(f"Error clearing existing content under parent page: {e}")
        sys.exit(1)

    try:
        add_root_update_block(parent_page_id, notion_token)
    except Exception as e:
        print(f"Error updating root page: {e}")
        sys.exit(1)

    if markdown_path.is_dir():
        # Directory mode driven by docs.json
        try:
            counters = process_directory_with_docs_json(
                markdown_path, parent_page_id, notion_token
            )
            # counters already printed inside; nothing else to do
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    elif markdown_path.is_file():
        # Single-file mode (still supported)
        # Build URL without extension, treating parent dir as root_dir
        root_dir = markdown_path.parent.resolve()
        docs_url = build_docs_url(root_dir, markdown_path.resolve())

        try:
            page = create_notion_page_from_markdown(
                markdown_path,
                parent_page_id,
                notion_token,
                docs_url=docs_url,
            )
            url = page.get("url", "(no url in response)")
            print(f"Notion page created successfully: {url}")
            print("[stats] Total content pages created: 1")
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        print(f"Error: Path not found: {markdown_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
