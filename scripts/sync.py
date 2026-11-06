#!/usr/bin/env python3
"""GitHub -> Confluence one-way documentation synchronizer.

Reconciles the Markdown documents under docs/ with a dedicated subtree of a
Confluence Cloud space. Pages are matched to documents by a stable ``doc_id``
declared in each document's YAML front matter and stored as a Confluence
content property on every managed page. Nothing is ever committed back to the
repository.

Environment variables (required):
  ATLASSIAN_BASE_URL        e.g. https://example.atlassian.net
  ATLASSIAN_USER_EMAIL      account email for Basic auth
  ATLASSIAN_API_TOKEN       Atlassian Cloud API token
  CONFLUENCE_SPACE_KEY      e.g. GHConfDocs
  CONFLUENCE_ROOT_PAGE_ID   page id of this repository's root page
  GITHUB_REPOSITORY         e.g. owner/confluence-docs-alpha  (page ownership tag)

Behaviour flags (CLI or env):
  --repo-root PATH                     content repository root (required)
  --dry-run / DRY_RUN=true             report the plan, mutate nothing
  --force-delete / FORCE_DELETE=true   override the mass-deletion safeguard
  --skip-mermaid                       tolerate unrendered mermaid blocks (local use)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import posixpath
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover
    print("ERROR: markdown-it-py is required. Run: pip install -r requirements.txt",
          file=sys.stderr)
    raise SystemExit(2)

TOOL = "gh-confluence-sync-poc/1.0"
PROP_KEY = "ghdocsync"          # content property key on every managed page
ATTACH_PREFIX = "ghsync-"       # only attachments with this prefix are ever removed
MAX_AUTO_DELETES = 5            # block if more pages than this would be deleted
MAX_DELETE_RATIO = 0.20         # ...or more than this fraction of managed pages
INDEX_FILE = "_index.md"        # optional folder descriptor (front matter + body)

DOC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,127}$")


class SyncError(Exception):
    pass


class ApiError(SyncError):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body[:600]
        super().__init__(f"HTTP {status} from {url}: {self.body}")


class AuthError(SyncError):
    pass


# ---------------------------------------------------------------------------
# Confluence REST client (v2 primary, v1 fallback for attachments/properties)
# ---------------------------------------------------------------------------

class Confluence:
    RETRY_STATUS = {429, 502, 503, 504}

    def __init__(self, base_url: str, email: str, token: str):
        self.base = base_url.rstrip("/")
        self.auth_header = "Basic " + base64.b64encode(
            f"{email}:{token}".encode()).decode()

    def _request(self, method: str, url: str, *, payload=None, raw=None,
                 headers=None, is_json=True):
        full_url = url if url.startswith("http") else self.base + url
        hdrs = {"Authorization": self.auth_header, "User-Agent": TOOL,
                "Accept": "application/json"}
        data = None
        if raw is not None:
            data = raw
            hdrs.update(headers or {})
        elif payload is not None:
            data = json.dumps(payload).encode()
            hdrs["Content-Type"] = "application/json"
        last_err = None
        for attempt in range(4):
            req = urllib.request.Request(full_url, data=data, headers=hdrs,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    return json.loads(body) if body and is_json else (body or "")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 401:
                    raise AuthError(
                        "Confluence rejected the credentials (HTTP 401). Check "
                        "ATLASSIAN_USER_EMAIL and ATLASSIAN_API_TOKEN.")
                if e.code in self.RETRY_STATUS:
                    last_err = ApiError(e.code, full_url, body)
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(e.code, full_url, body)
            except urllib.error.URLError as e:
                if attempt < 3:
                    last_err = SyncError(f"network error calling {full_url}: {e}")
                    time.sleep(2 ** attempt)
                    continue
                raise SyncError(f"network error calling {full_url}: {e}. "
                                f"Is ATLASSIAN_BASE_URL correct and reachable?")
        raise last_err or SyncError("request failed")

    def get(self, url):
        return self._request("GET", url)

    def post(self, url, payload=None, **kw):
        return self._request("POST", url, payload=payload, **kw)

    def put(self, url, payload=None, **kw):
        return self._request("PUT", url, payload=payload, **kw)

    def delete(self, url):
        return self._request("DELETE", url)

    def get_all(self, first_url: str):
        out, url = [], first_url
        while url:
            resp = self.get(url)
            out.extend(resp.get("results", []))
            url = resp.get("links", {}).get("next")
        return out

    # -- spaces / pages -----------------------------------------------------
    def space_by_key(self, key: str):
        try:
            return self.get(f"/wiki/rest/api/space/{urllib.parse.quote(key)}")
        except ApiError as e:
            if e.status == 404:
                raise SyncError(f"space key '{key}' not found (CONFLUENCE_SPACE_KEY)")
            raise

    def get_page(self, page_id: str):
        return self.get(f"/wiki/api/v2/pages/{page_id}")

    def pages_in_space(self, space_id: str):
        return self.get_all(f"/wiki/api/v2/spaces/{space_id}/pages?limit=250")

    def create_page(self, space_id: str, title: str, parent_id, body: str):
        payload = {"spaceId": space_id, "status": "current", "title": title,
                   "body": {"representation": "storage", "value": body}}
        if parent_id:
            payload["parentId"] = str(parent_id)
        return self.post("/wiki/api/v2/pages", payload)

    def update_page(self, page_id: str, title: str, body: str):
        payload = {"id": str(page_id), "status": "current", "title": title,
                   "body": {"representation": "storage", "value": body}}
        try:
            return self.put(f"/wiki/api/v2/pages/{page_id}", payload)
        except ApiError as e:
            if e.status not in (400, 409):
                raise
            cur = self.get_page(page_id)
            payload["version"] = {"number": cur["version"]["number"] + 1}
            return self.put(f"/wiki/api/v2/pages/{page_id}", payload)

    def move_page(self, page_id: str, new_parent_id: str):
        """Move a page under a new parent (v2 endpoint first, v1 fallback)."""
        try:
            return self.post(f"/wiki/api/v2/pages/{page_id}/move",
                             {"parentId": str(new_parent_id)})
        except ApiError as e:
            if e.status not in (400, 404, 405):
                raise
        cur = self.get(f"/wiki/rest/api/content/{page_id}?expand=version,body.storage")
        payload = {"id": str(page_id), "type": "page", "title": cur["title"],
                   "ancestors": [{"id": str(new_parent_id)}],
                   "body": {"storage": {"value": cur["body"]["storage"]["value"],
                                        "representation": "storage"}},
                   "version": {"number": cur["version"]["number"] + 1}}
        return self.put(f"/wiki/rest/api/content/{page_id}", payload)

    def delete_page(self, page_id: str):
        """Move a page to the trash (reversible). Permanent purge is never done."""
        try:
            return self.delete(f"/wiki/api/v2/pages/{page_id}")
        except ApiError as e:
            if e.status not in (400, 404, 405):
                raise
            return self.delete(f"/wiki/rest/api/content/{page_id}")

    # -- content properties (v1 endpoints; explicitly long-term supported) --
    def get_property(self, page_id: str):
        try:
            return self.get(f"/wiki/rest/api/content/{page_id}/property/{PROP_KEY}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def set_property(self, page_id: str, value: dict):
        url = f"/wiki/rest/api/content/{page_id}/property/{PROP_KEY}"
        body = {"value": value}
        current = self.get_property(page_id)
        if current is None:
            return self.post(url, body)
        body["version"] = {"number": current["version"]["number"] + 1}
        return self.put(url, body)

    # -- attachments ---------------------------------------------------------
    def list_attachments(self, page_id: str):
        try:
            res = self.get_all(f"/wiki/api/v2/pages/{page_id}/attachments?limit=200")
        except ApiError:
            res = self.get_all(
                f"/wiki/rest/api/content/{page_id}/child/attachment?limit=200")
        out = []
        for a in res:
            if isinstance(a, dict) and a.get("id") and a.get("title"):
                out.append({"id": str(a["id"]), "title": a["title"]})
        return out

    def upload_attachment(self, page_id: str, filename: str, content: bytes,
                          content_type: str):
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="minorEdit"'
                 f'\r\n\r\ntrue\r\n'.encode()]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
            .encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "X-Atlassian-Token": "no-check"}
        try:
            return self._request("POST",
                                 f"/wiki/api/v2/pages/{page_id}/attachments",
                                 raw=b"".join(parts), headers=headers)
        except (ApiError, SyncError):
            return self._request(
                "POST", f"/wiki/rest/api/content/{page_id}/child/attachment",
                raw=b"".join(parts), headers=headers)

    def delete_attachment(self, attachment_id: str):
        try:
            return self.delete(f"/wiki/api/v2/attachments/{attachment_id}")
        except ApiError as e:
            if e.status not in (400, 404, 405):
                raise
            return self.delete(f"/wiki/rest/api/content/{attachment_id}")


# ---------------------------------------------------------------------------
# Front matter + manifest
# ---------------------------------------------------------------------------

def parse_front_matter(text: str, path: str):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SyncError(f"{path}: missing YAML front matter (expected a '---' "
                        f"block with doc_id and title)")
    meta = {}
    for i, line in enumerate(lines[1:], start=1):
        s = line.strip()
        if s == "---":
            return meta, "\n".join(lines[i + 1:])
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            raise SyncError(f"{path}: cannot parse front matter line {line!r}")
        k, v = s.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    raise SyncError(f"{path}: front matter not terminated with '---'")


def norm_path(p: str) -> str:
    return posixpath.normpath(pathlib.PurePosixPath(p).as_posix()).lstrip("./")


def humanize_dirname(rel_dir: str) -> str:
    name = posixpath.basename(rel_dir.rstrip("/"))
    return re.sub(r"[-_]+", " ", name).strip().title()


def folder_synthetic_id(rel_dir: str) -> str:
    return "f" + hashlib.sha1(rel_dir.encode()).hexdigest()[:31]


class Manifest:
    def __init__(self, docs_rel: str):
        self.docs_rel = docs_rel          # e.g. "docs" (maps to the Confluence root)
        self.docs = {}                    # doc_id -> entry
        self.folders = {}                 # folder id -> entry (folders are pages too)
        self.entries = {}                 # every managed page identity -> entry
        self.titles = {}                  # title -> source (uniqueness check)

    def add(self, entry):
        if entry["id"] in self.entries:
            other = self.entries[entry["id"]]["source_path"]
            raise SyncError(f"duplicate identity {entry['id']} used by both "
                            f"{other} and {entry['source_path']}")
        title = entry["title"]
        if title in self.titles:
            raise SyncError(f"duplicate title {title!r} in {self.titles[title]} and "
                            f"{entry['source_path']}; page titles must be unique in "
                            f"the Confluence space (internal links resolve by title)")
        self.titles[title] = entry["source_path"]
        self.entries[entry["id"]] = entry
        (self.folders if entry["kind"] == "folder" else self.docs)[entry["id"]] = entry


def build_manifest(docs_dir: pathlib.Path, repo_root: pathlib.Path,
                   docs_rel: str) -> Manifest:
    m = Manifest(docs_rel)
    if not docs_dir.is_dir():
        raise SyncError(f"docs directory not found: {docs_dir}")
    folder_docs = {}  # rel_dir -> (doc_id, title, body, source_path)
    for p in sorted(docs_dir.rglob("*.md")):
        rel = norm_path(p.relative_to(repo_root))
        meta, body = parse_front_matter(p.read_text(encoding="utf-8"), rel)
        doc_id = meta.get("doc_id", "")
        title = meta.get("title", "")
        if not DOC_ID_RE.match(doc_id or ""):
            raise SyncError(f"{rel}: front matter must contain a valid doc_id "
                            f"(letters/digits/dots/dashes/underscores; got {doc_id!r})")
        if not title:
            raise SyncError(f"{rel}: front matter must contain a title")
        if p.name == INDEX_FILE:
            folder_docs[posixpath.dirname(rel)] = (doc_id, title, body, rel)
        else:
            m.add({"id": doc_id, "kind": "doc", "title": title,
                   "source_path": rel, "dir": posixpath.dirname(rel),
                   "markdown": body})

    # every directory that contains managed documents becomes a folder page
    doc_dirs = {e["dir"] for e in m.docs.values() if e["dir"]}
    needed_dirs = set(folder_docs) | doc_dirs
    for rel_dir in sorted(needed_dirs, key=len):
        if rel_dir == docs_rel or rel_dir == posixpath.dirname(docs_rel):
            continue  # the docs root itself maps onto the Confluence root page
        if rel_dir in folder_docs:
            doc_id, title, body, src = folder_docs[rel_dir]
            m.add({"id": doc_id, "kind": "folder", "title": title,
                   "source_path": src, "dir_path": rel_dir,
                   "dir": posixpath.dirname(rel_dir), "markdown": body})
        else:
            m.add({"id": folder_synthetic_id(rel_dir), "kind": "folder",
                   "title": humanize_dirname(rel_dir), "source_path": rel_dir + "/",
                   "dir_path": rel_dir, "dir": posixpath.dirname(rel_dir),
                   "markdown": None})
    # auto folders may need intermediate parents (docs/a/b without docs/a docs)
    changed = True
    while changed:
        changed = False
        for f in list(m.folders.values()):
            d = f["dir_path"]
            parent = posixpath.dirname(d)
            if parent in ("", docs_rel, posixpath.dirname(docs_rel)):
                continue
            if not any(g["dir_path"] == parent for g in m.folders.values()):
                m.add({"id": folder_synthetic_id(parent), "kind": "folder",
                       "title": humanize_dirname(parent), "source_path": parent + "/",
                       "dir_path": parent, "dir": posixpath.dirname(parent),
                       "markdown": None})
                changed = True
    return m


# ---------------------------------------------------------------------------
# Markdown -> Confluence storage-format conversion
# ---------------------------------------------------------------------------

CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(text: str) -> str:
    return esc(text).replace('"', "&quot;")


def slugify(name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "file")[:40]


class Converter:
    def __init__(self, manifest: Manifest, repo_root: pathlib.Path, space_key: str,
                 mermaid_map: dict, skip_mermaid: bool):
        self.m = manifest
        self.repo_root = repo_root
        self.space_key = space_key
        self.mermaid_map = mermaid_map      # sha256(source) -> svg bytes
        self.skip_mermaid = skip_mermaid
        self.md = MarkdownIt("gfm-like", {"html": False})
        self.md.disable("linkify")  # avoids the optional linkify-it-py dependency
        self.errors = []

    def fail(self, src, msg):
        self.errors.append(f"{src}: {msg}")

    def convert(self, entry) -> tuple[str, dict]:
        """Return (storage_xhtml, {attachment_name: (bytes, content_type)})."""
        self.attachments = {}
        if entry["markdown"] is None:
            return (f"<p><em>Folder page auto-generated from directory "
                    f"<code>{esc(entry['dir_path'])}</code>. Child pages are "
                    f"synchronized from the repository.</em></p>", {})
        blocks = self.md.parse(entry["markdown"])
        return self.render_blocks(blocks, entry), self.attachments

    def add_attachment(self, display_name: str, data: bytes) -> str:
        ext = pathlib.Path(display_name).suffix.lower() or ".bin"
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        name = (f"{ATTACH_PREFIX}{slugify(posixpath.basename(display_name))}-"
                f"{hashlib.sha1(data).hexdigest()[:8]}{ext}")
        self.attachments[name] = (data, ctype)
        return name

    # -- block renderer ------------------------------------------------------
    def render_blocks(self, tokens, entry) -> str:
        out, i = [], 0
        while i < len(tokens):
            tok = tokens[i]
            t = tok.type
            if t.endswith("_open"):
                close = t[:-5] + "_close"
                j = i + 1
                while not (tokens[j].type == close and tokens[j].level == tok.level):
                    j += 1
                inner = self.render_blocks(tokens[i + 1:j], entry)
                out.append(self.render_container(tok, inner))
                i = j + 1
                continue
            if t == "fence":
                out.append(self.render_fence(tok, entry))
            elif t == "code_block":
                out.append(self.code_macro(None, tok.content))
            elif t == "hr":
                out.append("<hr/>")
            elif t == "inline":
                out.append(self.render_inline(tok.children, entry))
            i += 1
        return "".join(out)

    def render_container(self, tok, inner: str) -> str:
        t, tag = tok.type, tok.tag
        if t in ("paragraph_open", "heading_open"):
            if tok.hidden:
                return inner
            return f"<{tag}>{inner}</{tag}>" if inner else ""
        if t in ("bullet_list_open", "ordered_list_open"):
            start = ""
            if t == "ordered_list_open" and tok.attrs:
                s = dict(tok.attrs).get("start")
                if s:
                    start = f' start="{esc_attr(str(s))}"'
            return f"<{tag}{start}>{inner}</{tag}>"
        if t == "list_item_open":
            return f"<li>{inner}</li>"
        if t == "blockquote_open":
            return f"<blockquote>{inner}</blockquote>"
        if t in ("thead_open", "tbody_open"):
            return inner
        if t == "tr_open":
            return f"<tr>{inner}</tr>"
        if t in ("th_open", "td_open"):
            return f"<{tag}>{inner}</{tag}>"
        if t == "table_open":
            return f'<table class="wrapped"><tbody>{inner}</tbody></table>'
        return inner

    def render_fence(self, tok, entry) -> str:
        lang = ((tok.info or "").strip().split() or [""])[0]
        if lang == "mermaid":
            return self.mermaid_macro(tok.content, entry["source_path"])
        return self.code_macro(lang or None, tok.content)

    def code_macro(self, lang, content: str) -> str:
        cdata = content.replace("]]>", "]]]]><![CDATA[>")
        lang_param = (f'<ac:parameter ac:name="language">{esc(lang)}</ac:parameter>'
                      if lang else "")
        return ('<ac:structured-macro ac:name="code" ac:schema-version="1">'
                + lang_param
                + '<ac:parameter ac:name="collapse">false</ac:parameter>'
                + f'<ac:plain-text-body><![CDATA[{cdata}]]></ac:plain-text-body>'
                + '</ac:structured-macro>')

    def mermaid_macro(self, source: str, src) -> str:
        h = hashlib.sha256(source.encode()).hexdigest()
        svg = self.mermaid_map.get(h)
        if svg is None:
            if self.skip_mermaid:
                svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="520" '
                       b'height="60"><rect width="100%" height="100%" fill="#f4f5f7"/>'
                       b'<text x="12" y="34" font-family="sans-serif" font-size="13">'
                       b'Mermaid diagram (placeholder: not rendered in this local '
                       b'dry-run)</text></svg>')
            else:
                self.fail(src, "mermaid block was not rendered; run "
                               "scripts/render_mermaid.py first")
                return "<p><em>[mermaid render missing]</em></p>"
        name = self.add_attachment(f"mermaid-{h[:8]}.svg", svg)
        return (f'<p><ac:image ac:width="620">'
                f'<ri:attachment ri:filename="{esc_attr(name)}"/></ac:image></p>')

    # -- inline renderer -----------------------------------------------------
    def render_inline(self, children, entry, depth=0) -> str:
        out, i = [], 0
        children = children or []
        while i < len(children):
            tok = children[i]
            t = tok.type
            if t in ("text", "html_inline"):
                out.append(esc(tok.content))
            elif t == "code_inline":
                out.append(f"<code>{esc(tok.content)}</code>")
            elif t == "softbreak":
                out.append("\n")
            elif t == "hardbreak":
                out.append("<br/>")
            elif t in ("strong_open", "em_open", "s_open"):
                close = t[:-5] + "_close"
                j = i + 1
                while children[j].type != close:
                    j += 1
                inner = self.render_inline(children[i + 1:j], entry, depth + 1)
                out.append(f"<{tok.tag}>{inner}</{tok.tag}>")
                i = j + 1
                continue
            elif t == "link_open":
                j = i + 1
                while children[j].type != "link_close":
                    j += 1
                inner = self.render_inline(children[i + 1:j], entry, depth + 1)
                href = tok.attrGet("href") or ""
                out.append(self.render_link(href, inner, entry))
                i = j + 1
                continue
            elif t == "image":
                alt = esc(tok.content or "")
                out.append(self.render_image(tok.attrGet("src") or "", alt, entry))
            i += 1
        return "".join(out)

    def render_link(self, href: str, inner: str, entry) -> str:
        src = entry["source_path"]
        if re.match(r"^(https?://|mailto:)", href, re.I):
            return f'<a class="external-link" href="{esc_attr(href)}">{inner}</a>'
        if href.startswith("#"):
            return f'<a href="{esc_attr(href)}">{inner}</a>'
        target, _, anchor = href.partition("#")
        resolved = norm_path(posixpath.join(posixpath.dirname(src), target))
        doc_id = self.m.docs and {
            e["source_path"]: d for d, e in self.m.docs.items()}.get(resolved)
        if doc_id is None:
            # folder descriptors are also valid link targets
            doc_id = next((f_id for f_id, f in self.m.folders.items()
                           if f["source_path"] == resolved), None)
        if doc_id is None:
            self.fail(src, f"internal link {href!r} does not resolve to a managed "
                           f"document (looked for {resolved})")
            return f"<span>{inner}</span>"
        target_title = self.m.entries[doc_id]["title"]
        anchor_param = (f'<ac:link-parameter ac:key="anchor">{esc(anchor)}'
                        f'</ac:link-parameter>' if anchor else "")
        body = f'<ac:plain-text-link-body><![CDATA[{inner}]]></ac:plain-text-link-body>'
        return (f'<ac:link><ri:page ri:content-title="{esc_attr(target_title)}" '
                f'ri:space-key="{esc_attr(self.space_key)}"/>{anchor_param}{body}'
                f'</ac:link>')

    def render_image(self, src_url: str, alt: str, entry) -> str:
        src = entry["source_path"]
        if re.match(r"^https?://", src_url, re.I):
            return f'<ac:image><ri:url ri:value="{esc_attr(src_url)}"/></ac:image>'
        resolved = norm_path(posixpath.join(posixpath.dirname(src), src_url))
        fspath = self.repo_root.joinpath(*resolved.split("/"))
        if not fspath.is_file():
            self.fail(src, f"image {src_url!r} not found at {resolved}")
            return f"<p><em>[missing image {esc(src_url)}]</em></p>"
        name = self.add_attachment(posixpath.basename(resolved), fspath.read_bytes())
        return (f'<ac:image ac:alt="{esc_attr(alt)}">'
                f'<ri:attachment ri:filename="{esc_attr(name)}"/></ac:image>')


# ---------------------------------------------------------------------------
# Desired-state reconciliation
# ---------------------------------------------------------------------------

def desired_parent_key(entry, manifest: Manifest) -> str:
    """Identity of the parent page for an entry: a folder id or 'ROOT'."""
    folder_by_dir = {f["dir_path"]: fid for fid, f in manifest.folders.items()}
    # A folder's dir_path is itself; dir is its parent for both kinds.
    d = entry["dir"]
    while d and d not in ("", posixpath.dirname(manifest.docs_rel)):
        if d in folder_by_dir:
            return folder_by_dir[d]
        d = posixpath.dirname(d)
    return "ROOT"


def creation_order(creates):
    """Return create operations ordered so every new parent precedes its children."""
    by_id = {entry["id"]: (entry, parent_key) for entry, parent_key in creates}
    ordered, done, visiting = [], set(), set()

    def add(entry, parent_key):
        identity = entry["id"]
        if identity in done:
            return
        if identity in visiting:
            raise SyncError(f"cyclic parent relationship involving {identity}")
        visiting.add(identity)
        if parent_key in by_id:
            add(*by_id[parent_key])
        visiting.remove(identity)
        done.add(identity)
        ordered.append((entry, parent_key))

    for entry, parent_key in sorted(creates, key=lambda c: c[0]["source_path"]):
        add(entry, parent_key)
    return ordered


def prop_value(repo: str, entry) -> dict:
    return {"repo": repo, "kind": entry["kind"], "doc_id": entry["id"],
            "source_path": entry["source_path"],
            "content_hash": entry["content_hash"], "synced_at": now_iso(),
            "tool": TOOL}


def descendants_of(pages_by_id: dict, parent_map: dict, root_id: str):
    def is_descendant(pid):
        cur, seen = pid, set()
        while cur and cur not in seen:
            if cur == root_id:
                return True
            seen.add(cur)
            cur = parent_map.get(cur, "")
        return False
    return {pid for pid in pages_by_id if is_descendant(pid)}


def build_plan(manifest: Manifest, confluence: Confluence, space_id: str,
               root_id: str, repo: str):
    pages = confluence.pages_in_space(space_id)
    by_id = {str(p["id"]): p for p in pages if p.get("status") == "current"}
    parent = {pid: str(p.get("parentId") or "") for pid, p in by_id.items()}
    descendant_ids = descendants_of(by_id, parent, root_id)

    managed = {}  # identity -> {"pid","page","prop"}
    for pid in descendant_ids:
        prop_resp = confluence.get_property(pid)
        if not prop_resp:
            continue  # unmanaged (manually created) page: never touched
        value = prop_resp.get("value")
        if not isinstance(value, dict) or value.get("repo") != repo:
            continue  # managed by another repository: never touched
        identity = value.get("doc_id")
        if identity:
            managed[identity] = {"pid": pid, "page": by_id[pid], "prop": value}

    plan = {"creates": [], "updates": [], "moves": [], "deletes": [], "unchanged": []}
    for identity, entry in manifest.entries.items():
        cur = managed.get(identity)
        parent_key = desired_parent_key(entry, manifest)
        if cur is None:
            plan["creates"].append((entry, parent_key))
        else:
            if (entry["title"] != cur["page"]["title"]
                    or entry["content_hash"] != cur["prop"].get("content_hash")):
                plan["updates"].append((entry, cur))
            else:
                plan["unchanged"].append((entry, cur))
            plan["moves"].append((entry, cur, parent_key))
    for identity, info in managed.items():
        if identity not in manifest.entries:
            plan["deletes"].append((info["pid"], info["page"], info["prop"]))
    return plan, managed, by_id, parent


def check_delete_safety(plan, managed: dict, force: bool):
    n = len(plan["deletes"])
    if n == 0:
        return
    total = len(managed)
    cap_hit = n > MAX_AUTO_DELETES
    ratio_hit = total > 0 and (n / total) > MAX_DELETE_RATIO
    if (cap_hit or ratio_hit) and not force:
        reasons = []
        if cap_hit:
            reasons.append(f"more than {MAX_AUTO_DELETES} pages")
        if ratio_hit:
            reasons.append(f"more than {MAX_DELETE_RATIO:.0%} of the {total} "
                           f"pages managed by this repository")
        listing = "\n".join(f"  - {d[2].get('source_path', '?')} (page {d[0]})"
                            for d in plan["deletes"])
        raise SyncError(
            f"Mass-deletion safeguard: refusing to trash {n} page(s) "
            f"({' and '.join(reasons)}).\nProposed deletions:\n{listing}\n"
            f"If this refactor is intentional, re-run the workflow with the "
            f"'force_delete' input (or --force-delete) to authorise it.")


def publish_attachments(confluence: Confluence, page_id: str, entry):
    existing = {a["title"]: a["id"] for a in confluence.list_attachments(page_id)}
    for name, (data, ctype) in entry["attachments"].items():
        if name not in existing:
            confluence.upload_attachment(page_id, name, data, ctype)
    for title, att_id in existing.items():
        if title.startswith(ATTACH_PREFIX) and title not in entry["attachments"]:
            confluence.delete_attachment(att_id)


def load_mermaid_map(repo_root: pathlib.Path) -> dict:
    mf = repo_root / "build" / "mermaid" / "manifest.json"
    if not mf.is_file():
        return {}
    out = {}
    for h, info in json.loads(mf.read_text(encoding="utf-8")).items():
        svg = pathlib.Path(info["svg"])
        if svg.is_file():
            out[h] = svg.read_bytes()
    return out


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_step_summary(report, headline):
    sf = os.environ.get("GITHUB_STEP_SUMMARY")
    if not sf:
        return
    with open(sf, "a", encoding="utf-8") as f:
        f.write(f"\n## {headline}\n\n```\n" + "\n".join(report) + "\n```\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="GitHub -> Confluence docs sync")
    ap.add_argument("--dry-run", action="store_true",
                    default=os.environ.get("DRY_RUN", "").lower() == "true")
    ap.add_argument("--force-delete", action="store_true",
                    default=os.environ.get("FORCE_DELETE", "").lower() == "true")
    ap.add_argument("--skip-mermaid", action="store_true",
                    default=os.environ.get("SKIP_MERMAID", "").lower() == "true")
    ap.add_argument("--repo-root", required=True,
                    help="path to the content repository containing docs/")
    ap.add_argument("--docs-dir", default="docs")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    required = ["ATLASSIAN_BASE_URL", "ATLASSIAN_USER_EMAIL", "ATLASSIAN_API_TOKEN",
                "CONFLUENCE_SPACE_KEY", "CONFLUENCE_ROOT_PAGE_ID", "GITHUB_REPOSITORY"]
    missing = [n for n in required if not os.environ.get(n)]
    if missing:
        raise SyncError("missing required environment variables: " + ", ".join(missing))

    base = os.environ["ATLASSIAN_BASE_URL"]
    repo = os.environ["GITHUB_REPOSITORY"]
    space_key = os.environ["CONFLUENCE_SPACE_KEY"]
    root_id = str(os.environ["CONFLUENCE_ROOT_PAGE_ID"]).strip()

    repo_root = pathlib.Path(args.repo_root).expanduser().resolve()
    docs_dir = repo_root / args.docs_dir
    docs_rel = norm_path(args.docs_dir)
    report, say = [], lambda line="": (print(line), report.append(line))

    confluence = Confluence(base, os.environ["ATLASSIAN_USER_EMAIL"],
                            os.environ["ATLASSIAN_API_TOKEN"])
    say(f"gh-confluence-sync repo={repo} space={space_key} root={root_id}"
        + (" DRY-RUN" if args.dry_run else ""))

    # 1. connect and validate the target space/root page
    space = confluence.space_by_key(space_key)
    space_id = str(space["id"])
    root_page = confluence.get_page(root_id)
    if str(root_page.get("spaceId", "")) != space_id:
        raise SyncError(f"root page {root_id} is not in space {space_key} (it lives "
                        f"in space {root_page.get('spaceId')}); check "
                        f"CONFLUENCE_ROOT_PAGE_ID")
    say(f"space '{space_key}' id={space_id}; root page "
        f"'{root_page['title']}' ({root_id})")

    # 2. build the desired state (manifest + converted bodies)
    manifest = build_manifest(docs_dir, repo_root, docs_rel)
    if not manifest.docs:
        raise SyncError("no managed documents found under docs/ (every .md file "
                        "needs front matter with doc_id and title). Refusing to "
                        "reconcile an empty tree.")
    converter = Converter(manifest, repo_root, space_key,
                          load_mermaid_map(repo_root), args.skip_mermaid)
    for entry in manifest.entries.values():
        body, attachments = converter.convert(entry)
        entry["body"] = body
        entry["attachments"] = attachments
        entry["content_hash"] = hashlib.sha256(
            (entry["title"] + "\x00" + body).encode()).hexdigest()
    if converter.errors:
        for e in converter.errors:
            say("  ERROR " + e)
        raise SyncError(f"{len(converter.errors)} conversion error(s); Confluence "
                        f"was not modified")
    say(f"manifest: {len(manifest.docs)} documents, {len(manifest.folders)} "
        f"folder pages")

    # 3. discover current state and compute the plan
    plan, managed, by_id, parent = build_plan(manifest, confluence, space_id,
                                              root_id, repo)
    if managed and not manifest.entries and not args.force_delete:
        raise SyncError("refusing to remove every managed page; re-run with "
                        "force_delete if intentional")
    check_delete_safety(plan, managed, args.force_delete)

    say("")
    say("planned changes:")
    for entry, pk in sorted(plan["creates"], key=lambda c: c[0]["source_path"]):
        say(f"  CREATE  {entry['source_path']:52s} -> \"{entry['title']}\"")
    for entry, cur in plan["updates"]:
        say(f"  UPDATE  {entry['source_path']:52s} (page {cur['pid']})")
    for entry, cur, pk in plan["moves"]:
        say(f"  CHECK   {entry['source_path']:52s} hierarchy under {pk}")
    for pid, page, prop in plan["deletes"]:
        say(f"  DELETE  {prop.get('source_path', '?'):52s} (page {pid}) -> trash")
    if not (plan["creates"] or plan["updates"] or plan["deletes"]):
        say("  (none)")

    if args.dry_run:
        say("")
        say("DRY RUN: no changes were made to Confluence.")
        write_step_summary(report, "Confluence sync (dry run)")
        return

    # 4. execute: create parents-first, then updates, then moves
    stats = {"created": 0, "updated": 0, "moved": 0, "deleted": 0,
             "unchanged": len(plan["unchanged"])}
    page_id_of = {"ROOT": root_id}
    for identity, info in managed.items():
        page_id_of[identity] = info["pid"]  # existing pages keep their ids

    for entry, parent_key in creation_order(plan["creates"]):
        parent_page_id = page_id_of.get(parent_key)
        if parent_page_id is None:
            raise SyncError(f"internal error: parent {parent_key} not available "
                            f"when creating {entry['source_path']}")
        created = confluence.create_page(space_id, entry["title"],
                                         parent_page_id, entry["body"])
        pid = str(created["id"])
        page_id_of[entry["id"]] = pid
        publish_attachments(confluence, pid, entry)
        confluence.set_property(pid, prop_value(repo, entry))
        stats["created"] += 1
        say(f"created page {pid} \"{entry['title']}\" "
            f"(doc_id={entry['id'][:8]}, parent={parent_page_id})")

    for entry, cur in plan["updates"]:
        pid = cur["pid"]
        confluence.update_page(pid, entry["title"], entry["body"])
        publish_attachments(confluence, pid, entry)
        confluence.set_property(pid, prop_value(repo, entry))
        stats["updated"] += 1
        say(f"updated page {pid} \"{entry['title']}\"")

    pages_now = {str(p["id"]): p for p in confluence.pages_in_space(space_id)
                 if p.get("status") == "current"}
    updated_ids = {u[0]["id"] for u in plan["updates"]}
    for entry, cur, parent_key in plan["moves"]:
        pid = cur["pid"]
        desired_parent = page_id_of.get(parent_key)
        if desired_parent is None:
            raise SyncError(f"internal error: parent {parent_key} missing for move "
                            f"of {entry['source_path']}")
        if str(pages_now.get(pid, {}).get("parentId") or "") != str(desired_parent):
            confluence.move_page(pid, desired_parent)
            stats["moved"] += 1
            say(f"moved page {pid} \"{entry['title']}\" under page {desired_parent}")
        if (entry["id"] not in updated_ids
                and cur["prop"].get("source_path") != entry["source_path"]):
            confluence.set_property(pid, prop_value(repo, entry))
            say(f"updated source_path metadata for page {pid}")

    # attachments of unchanged pages are reconciled too (cheap no-op normally)
    for identity, entry in manifest.entries.items():
        if identity in {c[0]["id"] for c in plan["creates"]}:
            continue
        if identity in {u[0]["id"] for u in plan["updates"]}:
            continue
        if identity in managed and entry["attachments"]:
            publish_attachments(confluence, managed[identity]["pid"], entry)

    # 5. deletions last, re-verified against a fresh page map
    if plan["deletes"]:
        fresh = {str(p["id"]): p for p in confluence.pages_in_space(space_id)
                 if p.get("status") == "current"}
        fresh_parent = {pid: str(p.get("parentId") or "") for pid, p in fresh.items()}
        delete_ids = {pid for pid, _, _ in plan["deletes"]}

        def depth(pid):
            d, cur = 0, pid
            while cur and cur != root_id:
                cur = fresh_parent.get(cur, "")
                d += 1
            return d

        for pid, page, prop in sorted(plan["deletes"], key=lambda d: -depth(d[0])):
            if pid == root_id:
                say(f"skip delete: page {pid} is the repository root (never deleted)")
                continue
            if pid not in fresh:
                say(f"skip delete: page {pid} no longer exists")
                continue
            chain, cur = [], pid
            ok = False
            seen = set()
            while cur and cur not in seen:
                if cur == root_id:
                    ok = True
                    break
                seen.add(cur)
                cur = fresh_parent.get(cur, "")
            if not ok:
                say(f"skip delete: page {pid} is not under this repository's root")
                continue
            kids = [c for c, par in fresh_parent.items()
                    if par == pid and c not in delete_ids]
            if kids:
                say(f"skip delete: page {pid} still has child pages not part of "
                    f"this deletion ({', '.join(kids)}); refusing to harm them")
                continue
            fresh_prop = confluence.get_property(pid)
            if (not fresh_prop or not isinstance(fresh_prop.get("value"), dict)
                    or fresh_prop["value"].get("repo") != repo):
                say(f"skip delete: page {pid} lost its managed marker; refusing")
                continue
            confluence.delete_page(pid)
            stats["deleted"] += 1
            say(f"deleted (trashed) page {pid} \"{page['title']}\" "
                f"[{prop.get('source_path', '?')}]")

    say("")
    say("summary: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    write_step_summary(report, "Confluence sync")


if __name__ == "__main__":
    try:
        main()
    except (SyncError, AuthError) as e:
        print(f"\nSYNC FAILED: {e}", file=sys.stderr)
        sf = os.environ.get("GITHUB_STEP_SUMMARY")
        if sf:
            with open(sf, "a", encoding="utf-8") as f:
                f.write(f"\n## Confluence sync FAILED\n\n```\n{e}\n```\n")
        raise SystemExit(1)
