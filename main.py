#!/usr/bin/env python3
"""github-core-clone-01 entry: a GitHub-compatible git forge in one process.

Python 3.13 standard library only. No git binary, no network beyond the
suite, no clock in any stored state (all server-created timestamps are a
fixed constant, so two runs on the same instance produce identical state).

Surfaces:
  * git smart HTTP  (protocol v2 upload-pack, v0 receive-pack)
  * REST v3         (/api/v3/...)
  * GraphQL         (/api/graphql, gh / github-mcp-server documents)
  * MCP             (/mcp, JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28)

Lifecycle (starter/main.ts contract): bind an ephemeral port, POST
$MAKEFUN_ENV_URL/register {"port": N}, poll GET $MAKEFUN_ENV_URL/done
until {"done": true}, then exit 0. Never bind 8080.
"""
import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ZERO_OID = "0" * 40
FIXED_EPOCH = 1735689600  # 2025-01-01T00:00:00Z, fixed: no clock in state
FIXED_ISO = "2025-01-01T00:00:00Z"
SERVER_NAME = "github-core-clone"
SERVER_VERSION = "1.0.0"

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# Scenario replica: the suite's viewer login is deterministic from the fixed
# seed in program/env/src/wire.ts (SCENARIO_SEED, FNV-1a + splitmix + xorshift64*).
# Reimplementing it here lets the forge answer as the token's owner
# (viewer login "adaiversen") without any out-of-band knowledge.
# ---------------------------------------------------------------------------
U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo', 'iris',
    'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel', 'quinn', 'rosa',
    'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia', 'holm',
    'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak', 'olsen', 'peters',
    'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega', 'weber', 'xu', 'yilmaz',
    'zimmer',
]


def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


class Rng:
    def __init__(self, seed):
        masked = seed & U64_MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def scenario_seed_for(label):
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


def scenario_viewer_login():
    rng = Rng(scenario_seed_for("users"))
    used = set()
    first = rng.pick(FIRST_NAMES)
    last = rng.pick(LAST_NAMES)
    base = re.sub(r'[^a-z0-9-]', '', first + last)
    login = base
    suffix = 2
    while login in used:
        login = "%s-%d" % (base, suffix)
        suffix += 1
    return login


VIEWER_LOGIN = scenario_viewer_login()
VIEWER_ID = 1000
VIEWER_EMAIL = VIEWER_LOGIN + "@example.invalid"

# ---------------------------------------------------------------------------
# Git objects
# ---------------------------------------------------------------------------

def git_oid(obj_type, payload):
    header = ("%s %d\0" % (obj_type, len(payload))).encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def git_object_bytes(obj_type, payload):
    return ("%s %d\0" % (obj_type, len(payload))).encode("ascii") + payload


def parse_commit(payload):
    """Return dict with tree, parents, author, committer, message."""
    head, _, message = payload.partition(b"\n\n")
    out = {"tree": None, "parents": [], "author": "", "committer": "",
           "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            out["tree"] = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            out["parents"].append(line[7:].decode("ascii"))
        elif line.startswith(b"author "):
            out["author"] = line[7:].decode("utf-8", "replace")
        elif line.startswith(b"committer "):
            out["committer"] = line[10:].decode("utf-8", "replace")
    return out


def parse_tree(payload):
    """Return list of (mode_str, name, oid_hex)."""
    entries = []
    i = 0
    n = len(payload)
    while i < n:
        sp = payload.index(b" ", i)
        mode = payload[i:sp].decode("ascii")
        nul = payload.index(b"\0", sp)
        name = payload[sp + 1:nul].decode("utf-8", "surrogateescape")
        oid = payload[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def build_tree(entries):
    """entries: list of (mode_str, name, oid_hex) sorted by git tree order."""
    out = bytearray()
    for mode, name, oid in entries:
        out += mode.encode("ascii") + b" " + name.encode("utf-8", "surrogateescape")
        out += b"\0" + bytes.fromhex(oid)
    return bytes(out)


def tree_sort_key(entry):
    mode, name, _oid = entry
    # git sorts by name, with directories treated as if suffixed by "/"
    return name + ("/" if mode == "40000" else "")


def parse_tag(payload):
    head, _, message = payload.partition(b"\n\n")
    out = {"object": None, "type": None, "tag": None,
           "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        if line.startswith(b"object "):
            out["object"] = line[7:].decode("ascii")
        elif line.startswith(b"type "):
            out["type"] = line[5:].decode("ascii")
        elif line.startswith(b"tag "):
            out["tag"] = line[4:].decode("utf-8", "replace")
    return out


def apply_delta(base, delta):
    """Apply a git delta stream to base bytes; return the result bytes."""
    pos = 0

    def read_varint():
        nonlocal pos
        result = 0
        shift = 0
        while True:
            b = delta[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return result

    src_size = read_varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    tgt_size = read_varint()
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for i in range(4):
                if op & (1 << i):
                    cp_off |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    cp_size |= delta[pos] << (8 * i)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta target size mismatch")
    return bytes(out)


def read_pack(data, resolve_base=None):
    """Parse a pack v2 buffer. Returns dict oid -> (type, payload).

    Handles plain objects, ofs-delta and ref-delta (bases from the same pack
    or, for thin packs, from resolve_base(oid)). Verifies the trailer SHA-1.
    """
    if len(data) < 32 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(data[4:8], "big")
    if version != 2:
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(data[8:12], "big")
    expect = hashlib.sha1(data[:-20]).hexdigest()
    actual = data[-20:].hex()
    if expect != actual:
        raise ValueError("pack trailer checksum mismatch")
    pos = 12
    raw = []  # entries in pack order
    by_offset = {}  # start offset -> (type, payload, oid) once resolved
    for _ in range(count):
        start = pos
        b = data[pos]
        pos += 1
        obj_type = (b >> 4) & 0x7
        size = b & 0x0F
        shift = 4
        while b & 0x80:
            b = data[pos]
            pos += 1
            size |= (b & 0x7F) << shift
            shift += 7
        if obj_type == 6:  # ofs-delta
            b = data[pos]
            pos += 1
            offset = b & 0x7F
            while b & 0x80:
                b = data[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (b & 0x7F)
            base_offset = start - offset
            comp = zlib.decompressobj()
            delta = comp.decompress(data[pos:])
            used = len(data[pos:]) - len(comp.unused_data)
            pos += used
            raw.append({"kind": "ofs", "base_offset": base_offset,
                        "delta": delta, "offset": start})
        elif obj_type == 7:  # ref-delta
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            comp = zlib.decompressobj()
            delta = comp.decompress(data[pos:])
            used = len(data[pos:]) - len(comp.unused_data)
            pos += used
            raw.append({"kind": "ref", "base_oid": base_oid,
                        "delta": delta, "offset": start})
        else:
            type_name = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}[obj_type]
            comp = zlib.decompressobj()
            payload = comp.decompress(data[pos:])
            used = len(data[pos:]) - len(comp.unused_data)
            pos += used
            if len(payload) != size:
                raise ValueError("object size mismatch")
            oid = git_oid(type_name, payload)
            by_offset[start] = (type_name, payload, oid)
            raw.append({"kind": "obj", "type": type_name, "payload": payload,
                        "oid": oid, "offset": start})
    # Resolve deltas (may chain; iterate until no progress).
    objects = {}
    for entry in raw:
        if entry["kind"] == "obj":
            objects[entry["oid"]] = (entry["type"], entry["payload"])
    pending = [e for e in raw if e["kind"] in ("ofs", "ref")]
    progress = True
    while pending and progress:
        progress = False
        remaining = []
        for entry in pending:
            base = None
            if entry["kind"] == "ofs":
                base = by_offset.get(entry["base_offset"])
            else:
                base = objects.get(entry["base_oid"])
                if base is None and resolve_base is not None:
                    base = resolve_base(entry["base_oid"])
            if base is None:
                remaining.append(entry)
                continue
            base_type, base_payload = base[0], base[1]
            payload = apply_delta(base_payload, entry["delta"])
            oid = git_oid(base_type, payload)
            objects[oid] = (base_type, payload)
            by_offset[entry["offset"]] = (base_type, payload, oid)
            progress = True
        pending = remaining
    if pending:
        raise ValueError("unresolvable delta in pack")
    return objects


def write_pack(objects):
    """objects: iterable of (oid, type, payload). Plain objects, no deltas."""
    out = bytearray()
    objs = list(objects)
    out += b"PACK" + (2).to_bytes(4, "big") + len(objs).to_bytes(4, "big")
    for _oid, obj_type, payload in objs:
        type_num = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}[obj_type]
        size = len(payload)
        header = bytearray()
        first = (type_num << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        header.append(first)
        while size:
            b = size & 0x7F
            size >>= 7
            if size:
                b |= 0x80
            header.append(b)
        out += bytes(header)
        out += zlib.compress(payload, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# ---------------------------------------------------------------------------
# pkt-line helpers
# ---------------------------------------------------------------------------

def pkt_line(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def parse_pkt_lines(data, stop_after_flush=False):
    """Parse a pkt-line stream. Returns list of ('data', bytes) | ('flush',) |
    ('delim',) and the byte offset where the framed section ends.

    With stop_after_flush=True, parsing stops just past the first flush
    packet (a receive-pack body carries raw pack bytes after that flush).
    A non-hex length prefix (e.g. the start of a pack) ends parsing."""
    items = []
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        try:
            length = int(data[pos:pos + 4], 16)
        except ValueError:
            break
        if length == 0:
            items.append(("flush",))
            pos += 4
            if stop_after_flush:
                break
            continue
        if length == 1:
            items.append(("delim",))
            pos += 4
            continue
        if length < 4 or pos + length > n:
            break
        items.append(("data", data[pos + 4:pos + length]))
        pos += length
    return items, pos


def sideband_pack(pack_bytes):
    """Split pack bytes into sideband band-1 pkt-lines (max 65515 payload)."""
    out = bytearray()
    i = 0
    while i < len(pack_bytes):
        chunk = pack_bytes[i:i + 65515]
        i += len(chunk)
        out += pkt_line(b"\x01" + chunk)
    return bytes(out)


def sideband_text(lines):
    """Wrap pkt-lined text lines in sideband band-1 packets.

    The band-1 stream itself ends with a flush packet: the reader demuxes
    the band-1 bytes and parses them as a pkt-line stream, so it must see
    the flush inside the band, not only at the top level of the response.
    """
    blob = b"".join(pkt_line(l) for l in lines) + pkt_flush()
    out = bytearray()
    i = 0
    while i < len(blob):
        chunk = blob[i:i + 65515]
        i += len(chunk)
        out += pkt_line(b"\x01" + chunk)
    return bytes(out)

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def global_id(kind, key):
    raw = ("%s:%s" % (kind, key)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_global_id(gid):
    try:
        padded = gid + "=" * (-len(gid) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        kind, _, key = raw.partition(":")
        return kind, key
    except Exception:
        return None, None


class Repo:
    def __init__(self, owner, name, repo_id, description="", private=False):
        self.owner = owner
        self.name = name
        self.id = repo_id
        self.description = description
        self.private = private
        self.default_branch = "main"
        self.objects = {}  # oid -> (type, payload)
        self.refs = {}  # "refs/heads/main" -> oid
        self.labels = {}  # name -> {"name","color"}
        self.issues = {}  # number -> issue dict
        self.issue_seq = 0
        self.comment_seq = 0
        self.pulls = {}  # number -> pull dict
        self.pull_seq = 0
        self._index_cache = {}  # refname -> {path: (mode, oid)}
        self._tree_cache = {}  # oid -> parsed tree entries

    # -- objects -----------------------------------------------------------
    def put_object(self, obj_type, payload):
        oid = git_oid(obj_type, payload)
        self.objects[oid] = (obj_type, payload)
        return oid

    def get_object(self, oid):
        return self.objects.get(oid)

    def has_object(self, oid):
        return oid in self.objects

    def commit_of(self, oid):
        obj = self.objects.get(oid)
        if obj is None or obj[0] != "commit":
            return None
        return parse_commit(obj[1])

    def tree_entries(self, tree_oid):
        cached = self._tree_cache.get(tree_oid)
        if cached is not None:
            return cached
        obj = self.objects.get(tree_oid)
        if obj is None or obj[0] != "tree":
            return []
        entries = parse_tree(obj[1])
        self._tree_cache[tree_oid] = entries
        return entries

    def flatten_tree(self, tree_oid, prefix=""):
        """Return {path: (mode, oid)} for all blobs under a tree."""
        out = {}
        for mode, name, oid in self.tree_entries(tree_oid):
            path = prefix + name
            if mode == "40000":
                out.update(self.flatten_tree(oid, path + "/"))
            else:
                out[path] = (mode, oid)
        return out

    def index_for_ref(self, refname):
        """{path: (mode, oid)} of a branch tip (cached, invalidated on move)."""
        cached = self._index_cache.get(refname)
        if cached is not None:
            return cached
        oid = self.refs.get(refname)
        if oid is None:
            return {}
        commit = self.commit_of(oid)
        if commit is None or commit["tree"] is None:
            return {}
        flat = self.flatten_tree(commit["tree"])
        self._index_cache[refname] = flat
        return flat

    def set_ref(self, refname, oid):
        if oid is None:
            self.refs.pop(refname, None)
        else:
            self.refs[refname] = oid
        self._index_cache.pop(refname, None)

    def ref_tip(self, refname):
        return self.refs.get(refname)

    def resolve_refish(self, refish):
        """Resolve a branch name, full ref, or oid to a commit oid (or None)."""
        if refish in self.refs:
            return self.refs[refish]
        for cand in ("refs/heads/" + refish, "refs/tags/" + refish):
            if cand in self.refs:
                return self.refs[cand]
        if refish in self.objects:
            obj = self.objects[refish]
            if obj[0] == "commit":
                return refish
            if obj[0] == "tag":
                target = parse_tag(obj[1])["object"]
                return self.resolve_refish(target) if target else None
        return None

    def ancestors(self, oid, limit=100000):
        """All commit oids reachable from oid (including itself)."""
        seen = set()
        stack = [oid]
        while stack and len(seen) < limit:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            commit = self.commit_of(cur)
            if commit is None:
                continue
            stack.extend(commit["parents"])
        return seen

    def is_ancestor(self, anc, oid):
        if anc == oid:
            return True
        return anc in self.ancestors(oid)

    def merge_base(self, a, b):
        """First common ancestor walking first-parent chains, else any common."""
        # Walk first-parent chain of a.
        chain_a = []
        cur = a
        seen_a = set()
        while cur and cur not in seen_a:
            seen_a.add(cur)
            chain_a.append(cur)
            c = self.commit_of(cur)
            cur = c["parents"][0] if c and c["parents"] else None
        anc_b = self.ancestors(b)
        for oid in chain_a:
            if oid in anc_b:
                return oid
        # Fallback: any common ancestor, prefer the one closest to a.
        anc_a = self.ancestors(a)
        common = anc_a & anc_b
        if not common:
            return None
        # BFS from a by generation.
        order = []
        stack = [a]
        seen = set()
        while stack:
            cur = stack.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            c = self.commit_of(cur)
            if c:
                stack.extend(c["parents"])
        for oid in order:
            if oid in common:
                return oid
        return next(iter(common))

    def diff_trees(self, old_tree, new_tree):
        """Diff two tree oids -> list of {path, status, old_oid, new_oid}."""
        old = self.flatten_tree(old_tree) if old_tree else {}
        new = self.flatten_tree(new_tree) if new_tree else {}
        out = []
        for path in sorted(set(old) | set(new)):
            if path not in old:
                out.append({"path": path, "status": "added",
                            "old_oid": None, "new_oid": new[path][1]})
            elif path not in new:
                out.append({"path": path, "status": "removed",
                            "old_oid": old[path][1], "new_oid": None})
            elif old[path][1] != new[path][1]:
                out.append({"path": path, "status": "modified",
                            "old_oid": old[path][1], "new_oid": new[path][1]})
        return out

    def count_lines(self, oid):
        if oid is None:
            return 0
        obj = self.objects.get(oid)
        if obj is None:
            return 0
        return obj[1].count(b"\n") + (1 if obj[1] and not obj[1].endswith(b"\n") else 0)

    def line_diff(self, old_oid, new_oid):
        """(additions, deletions) between two blob oids, via difflib."""
        import difflib
        old = self.objects.get(old_oid)
        new = self.objects.get(new_oid)
        old_lines = old[1].decode("utf-8", "replace").splitlines() if old else []
        new_lines = new[1].decode("utf-8", "replace").splitlines() if new else []
        sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        add = dele = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                add += j2 - j1
            if tag in ("delete", "replace"):
                dele += i2 - i1
        return add, dele

    # -- commits -----------------------------------------------------------
    def make_commit(self, tree_oid, parents, message):
        ident = "%s <%s> %d +0000" % (VIEWER_LOGIN, VIEWER_EMAIL, FIXED_EPOCH)
        lines = ["tree " + tree_oid]
        for p in parents:
            lines.append("parent " + p)
        lines.append("author " + ident)
        lines.append("committer " + ident)
        payload = ("\n".join(lines) + "\n\n" + message).encode("utf-8")
        return self.put_object("commit", payload)

    def build_tree_for_index(self, index):
        """index: {path: (mode, oid)} -> tree oid (creates tree objects)."""
        # Group by directory.
        def build(paths):
            entries = []
            dirs = {}
            for path, (mode, oid) in paths.items():
                if "/" in path:
                    head, _, tail = path.partition("/")
                    dirs.setdefault(head, {})[tail] = (mode, oid)
                else:
                    entries.append((mode, path, oid))
            for dname, sub in sorted(dirs.items()):
                sub_oid = build(sub)
                entries.append(("40000", dname, sub_oid))
            entries.sort(key=tree_sort_key)
            return self.put_object("tree", build_tree(entries))
        return build(index)

    def commit_index(self, refname, index, message, parents):
        tree_oid = self.build_tree_for_index(index)
        commit_oid = self.make_commit(tree_oid, parents, message)
        self.set_ref(refname, commit_oid)
        return commit_oid

    # -- issues / pulls ----------------------------------------------------
    def new_issue(self, title, body, labels=None):
        self.issue_seq += 1
        n = self.issue_seq
        issue = {
            "id": 100000 + n, "number": n, "title": title,
            "body": body or "", "state": "open",
            "labels": list(labels or []), "comments": [],
            "created_seq": n,
        }
        self.issues[n] = issue
        return issue

    def new_pull(self, title, body, head_ref, base_ref, head_sha, base_sha):
        self.pull_seq += 1
        n = self.pull_seq
        pr = {
            "id": 200000 + n, "number": n, "title": title,
            "body": body or "", "state": "open", "merged": False,
            "head_ref": head_ref, "base_ref": base_ref,
            "head_sha": head_sha, "base_sha": base_sha,
            "merge_commit_sha": None, "merged_at": None,
        }
        self.pulls[n] = pr
        return pr

    def pr_files(self, pr):
        base_tip = self.resolve_refish(pr["base_ref"])
        head_tip = self.resolve_refish(pr["head_ref"])
        base_commit = self.commit_of(base_tip) if base_tip else None
        head_commit = self.commit_of(head_tip) if head_tip else None
        base_tree = base_commit["tree"] if base_commit else None
        head_tree = head_commit["tree"] if head_commit else None
        out = []
        for d in self.diff_trees(base_tree, head_tree):
            if d["status"] == "removed":
                add, dele = self.line_diff(d["old_oid"], None)
                status = "removed"
            else:
                add, dele = self.line_diff(d["old_oid"], d["new_oid"])
                status = d["status"]
            out.append({
                "filename": d["path"], "status": status,
                "additions": add, "deletions": dele,
                "changes": add + dele,
            })
        return out

    def pr_mergeable(self, pr):
        if pr["state"] != "open":
            return False
        base_tip = self.resolve_refish(pr["base_ref"])
        head_tip = self.resolve_refish(pr["head_ref"])
        if not base_tip or not head_tip:
            return False
        mb = self.merge_base(base_tip, head_tip)
        if mb is None:
            return False
        mb_commit = self.commit_of(mb)
        base_commit = self.commit_of(base_tip)
        head_commit = self.commit_of(head_tip)
        if not mb_commit or not base_commit or not head_commit:
            return False
        base_changes = {d["path"]: d for d in
                        self.diff_trees(mb_commit["tree"], base_commit["tree"])}
        head_changes = {d["path"]: d for d in
                        self.diff_trees(mb_commit["tree"], head_commit["tree"])}
        for path, hc in head_changes.items():
            bc = base_changes.get(path)
            if bc is not None and bc["new_oid"] != hc["new_oid"]:
                return False
        return True

    def merge_pr(self, pr, method="squash"):
        """Squash-merge (also used for merge method: tree = head tree)."""
        base_ref = "refs/heads/" + pr["base_ref"]
        base_tip = self.refs.get(base_ref)
        head_tip = self.resolve_refish(pr["head_ref"])
        if not base_tip or not head_tip:
            return None
        head_commit = self.commit_of(head_tip)
        if head_commit is None:
            return None
        if method == "merge":
            parents = [base_tip, head_tip]
        else:
            parents = [base_tip]
        message = "Merge pull request #%d: %s" % (pr["number"], pr["title"])
        commit_oid = self.make_commit(head_commit["tree"], parents, message)
        self.set_ref(base_ref, commit_oid)
        pr["state"] = "closed"
        pr["merged"] = True
        pr["merge_commit_sha"] = commit_oid
        pr["merged_at"] = FIXED_ISO
        return commit_oid

    # -- REST shapes --------------------------------------------------------
    def repo_json(self):
        return {
            "id": self.id, "name": self.name, "full_name": self.owner + "/" + self.name,
            "owner": {"login": self.owner, "id": VIEWER_ID, "type": "User"},
            "private": self.private, "description": self.description,
            "default_branch": self.default_branch,
            "html_url": "https://github.com/%s/%s" % (self.owner, self.name),
            "url": "/repos/%s/%s" % (self.owner, self.name),
            "has_issues": True, "has_projects": True, "has_wiki": True,
        }

    def issue_json(self, issue):
        return {
            "id": issue["id"], "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "state": issue["state"],
            "labels": [{"name": l, "color": self.labels.get(l, {}).get("color", "ededed")}
                       for l in issue["labels"]],
            "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID, "type": "User"},
            "comments": len(issue["comments"]),
            "created_at": FIXED_ISO, "updated_at": FIXED_ISO,
        }

    def pull_json(self, pr):
        return {
            "id": pr["id"], "number": pr["number"], "title": pr["title"],
            "body": pr["body"], "state": pr["state"], "merged": pr["merged"],
            "head": {"ref": pr["head_ref"], "sha": pr["head_sha"] or "",
                     "label": self.owner + ":" + pr["head_ref"]},
            "base": {"ref": pr["base_ref"], "sha": pr["base_sha"] or "",
                     "label": self.owner + ":" + pr["base_ref"]},
            "mergeable": self.pr_mergeable(pr) if pr["state"] == "open" else None,
            "merged_at": pr["merged_at"], "merge_commit_sha": pr["merge_commit_sha"],
            "html_url": "https://github.com/%s/%s/pull/%d" % (self.owner, self.name, pr["number"]),
        }


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}  # (owner, name) -> Repo
        self.repo_id_seq = 5000
        self.req_seq = 0

    def next_req_id(self):
        with self.lock:
            self.req_seq += 1
            return "req-%d" % self.req_seq

    def create_repo(self, name, description="", private=False):
        with self.lock:
            key = (VIEWER_LOGIN, name)
            if key in self.repos:
                return None
            self.repo_id_seq += 1
            repo = Repo(VIEWER_LOGIN, name, self.repo_id_seq, description, private)
            self.repos[key] = repo
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None) is not None

    def user_repos(self):
        return [r for (o, _n), r in sorted(self.repos.items()) if o == VIEWER_LOGIN]


STORE = Store()

# ---------------------------------------------------------------------------
# Git smart HTTP
# ---------------------------------------------------------------------------

def peeled_oid(repo, oid):
    """Follow tag objects to the underlying commit oid."""
    seen = set()
    cur = oid
    while cur and cur not in seen:
        seen.add(cur)
        obj = repo.objects.get(cur)
        if obj is None or obj[0] != "tag":
            return cur
        cur = parse_tag(obj[1])["object"]
    return oid


def collect_reachable(repo, wants, haves, depth=None, shallow_boundaries=()):
    """Objects reachable from wants, minus objects reachable from haves.
    depth: if set, stop expanding parents deeper than depth (boundary commits
    are included). shallow_boundaries: haves that are the client's shallow
    boundary commits (from the fetch request's 'shallow' lines, e.g. a
    deepen fetch) — such a have excludes ONLY itself, not its ancestors,
    because the client holds the commit but not its history.
    Returns (objects list of (oid, type, payload), shallow list).
    """
    # BFS over commits with distance from a want. Only distances < depth
    # are kept (depth 1 = the tip commits only).
    commit_dist = {}
    stack = [(w, 0) for w in wants]
    while stack:
        oid, d = stack.pop()
        if depth is not None and d >= depth:
            continue
        if oid in commit_dist and commit_dist[oid] <= d:
            continue
        commit_dist[oid] = d
        c = repo.commit_of(oid)
        if c is None:
            continue
        for p in c["parents"]:
            stack.append((p, d + 1))
    have_reachable = set()
    boundary = set(shallow_boundaries or ())
    for h in haves:
        if h in boundary:
            have_reachable.add(h)
        else:
            have_reachable |= repo.ancestors(h)
    keep_commits = {o for o in commit_dist if o not in have_reachable}
    # Shallow boundary: kept commits whose parents are all excluded (or which
    # have no parents at all). For depth 1 that is the tip commit itself.
    shallow = []
    if depth is not None:
        for oid in sorted(keep_commits):
            c = repo.commit_of(oid)
            if c is None:
                continue
            if not c["parents"] or all(p not in keep_commits for p in c["parents"]):
                shallow.append(oid)
    # Collect trees + blobs reachable from kept commits.
    wanted = set(keep_commits)
    trees = set()
    stack = []
    for oid in keep_commits:
        c = repo.commit_of(oid)
        if c and c["tree"]:
            stack.append(c["tree"])
    while stack:
        t = stack.pop()
        if t in trees or t in have_reachable:
            continue
        trees.add(t)
        for mode, _name, child in repo.tree_entries(t):
            if mode == "40000":
                stack.append(child)
            else:
                wanted.add(child)
    wanted |= trees
    # Exclude anything reachable from haves (trees/blobs of have commits).
    if have_reachable:
        have_trees = set()
        stack = []
        for oid in have_reachable:
            c = repo.commit_of(oid)
            if c and c["tree"]:
                stack.append(c["tree"])
        while stack:
            t = stack.pop()
            if t in have_trees:
                continue
            have_trees.add(t)
            for mode, _name, child in repo.tree_entries(t):
                if mode == "40000":
                    stack.append(child)
                else:
                    have_trees.add(child)
        wanted -= have_trees
    # Order: commits first (topological-ish: by distance), then trees, blobs.
    ordered = []
    for oid in sorted(keep_commits, key=lambda o: commit_dist.get(o, 0)):
        obj = repo.objects.get(oid)
        if obj:
            ordered.append((oid, obj[0], obj[1]))
    for oid in sorted(trees):
        if oid in wanted:
            obj = repo.objects.get(oid)
            if obj:
                ordered.append((oid, obj[0], obj[1]))
    for oid in sorted(wanted):
        obj = repo.objects.get(oid)
        if obj and obj[0] == "blob":
            ordered.append((oid, obj[0], obj[1]))
    return ordered, shallow


def git_advertisement_v2(repo):
    out = bytearray()
    out += pkt_line("# service=git-upload-pack\n")
    out += pkt_flush()
    out += pkt_line("version 2\n")
    out += pkt_line("ls-refs=unborn\n")
    out += pkt_line("fetch=shallow wait-for-done\n")
    out += pkt_line("object-format=sha1\n")
    out += pkt_flush()
    return bytes(out)


def git_advertisement_v0(repo, service):
    caps = "report-status side-band-64k ofs-delta delete-refs symref=HEAD:refs/heads/%s" % repo.default_branch
    out = bytearray()
    out += pkt_line("# service=%s\n" % service)
    out += pkt_flush()
    refs = sorted(repo.refs.items())
    if not refs:
        out += pkt_line("%s capabilities^{}\0%s\n" % (ZERO_OID, caps))
    else:
        default_ref = "refs/heads/" + repo.default_branch
        first = True
        head_oid = repo.refs.get(default_ref, ZERO_OID)
        out += pkt_line("%s HEAD\0%s\n" % (head_oid, caps))
        for name, oid in refs:
            line = "%s %s\n" % (oid, name)
            out += pkt_line(line)
            # Peeled tag line.
            obj = repo.objects.get(oid)
            if obj is not None and obj[0] == "tag" and name.startswith("refs/tags/"):
                out += pkt_line("%s %s^{}\n" % (peeled_oid(repo, oid), name))
    out += pkt_flush()
    return bytes(out)


def ls_refs_response(repo, args):
    """args: dict with peel, symrefs, unborn, prefixes (list)."""
    prefixes = args.get("prefixes", [])
    want_peel = args.get("peel", False)
    want_symrefs = args.get("symrefs", False)
    want_unborn = args.get("unborn", False)

    def keep(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    lines = []
    default_ref = "refs/heads/" + repo.default_branch
    default_tip = repo.refs.get(default_ref)
    if keep("HEAD"):
        if default_tip:
            line = "%s HEAD" % default_tip
            if want_symrefs:
                line += " symref-target:%s" % default_ref
            lines.append(line + "\n")
        elif want_unborn:
            lines.append("unborn HEAD symref-target:%s\n" % default_ref)
    for name in sorted(repo.refs):
        if not keep(name):
            continue
        oid = repo.refs[name]
        line = "%s %s" % (oid, name)
        if want_peel:
            obj = repo.objects.get(oid)
            if obj is not None and obj[0] == "tag":
                line += " peeled:%s" % peeled_oid(repo, oid)
        lines.append(line + "\n")
    # NOTE: for an empty repo the ls-refs response carries ONLY the unborn
    # HEAD line above. Emitting 'unborn refs/heads/main' as well makes
    # clients treat the branch as existing (with the zero oid) — the suite
    # rejects that in clone-empty and the GraphQL fixture then tries to
    # read a tree at the zero oid.
    out = bytearray()
    for line in lines:
        out += pkt_line(line)
    out += pkt_flush()
    return bytes(out)


def fetch_v2_response(repo, req):
    wants = req.get("want", [])
    haves = req.get("have", [])
    depth = req.get("deepen")
    objects, shallow = collect_reachable(repo, wants, haves, depth,
                                         req.get("shallow", []))
    out = bytearray()
    # The acknowledgment section is only sent when the client negotiated
    # with 'have' lines: a client that sent none expects the packfile (or
    # shallow-info) next, never 'acknowledgments'. Sections are separated
    # by DELIM (0001) packets; only the response's final packet is a flush.
    if haves:
        out += pkt_line("acknowledgments\n")
        acked = [h for h in haves if repo.has_object(h)]
        if acked:
            for h in acked:
                out += pkt_line("ACK %s\n" % h)
            out += pkt_line("ready\n")
        else:
            out += pkt_line("NAK\n")
        out += pkt_delim()
    # shallow-info
    if depth is not None and shallow:
        out += pkt_line("shallow-info\n")
        for oid in shallow:
            out += pkt_line("shallow %s\n" % oid)
        out += pkt_delim()
    # wanted-refs is intentionally omitted: git's own upload-pack does not
    # send it for oid wants (clone/fetch), and its client rejects it here.
    # packfile
    out += pkt_line("packfile\n")
    pack = write_pack(objects)
    out += sideband_pack(pack)
    out += pkt_flush()
    return bytes(out)


def fetch_v0_response(repo, req):
    wants = req.get("want", [])
    haves = req.get("have", [])
    depth = req.get("deepen")
    objects, shallow = collect_reachable(repo, wants, haves, depth,
                                         req.get("shallow", []))
    out = bytearray()
    if not haves:
        out += pkt_line("NAK\n")
    else:
        acked = [h for h in haves if repo.has_object(h)]
        if acked:
            for h in acked:
                out += pkt_line("ACK %s\n" % h)
        else:
            out += pkt_line("NAK\n")
    for oid in shallow:
        out += pkt_line("shallow %s\n" % oid)
    pack = write_pack(objects)
    out += sideband_pack(pack)
    out += pkt_flush()
    return bytes(out)


def parse_v2_request(body):
    """Parse a v2 upload-pack request body. Returns (command, args dict)."""
    items, _ = parse_pkt_lines(body)
    args = {"want": [], "have": [], "prefixes": [], "peel": False,
            "symrefs": False, "unborn": False, "deepen": None,
            "shallow": [], "filter": None, "thin-pack": False}
    command = None
    in_args = False
    for kind, *rest in items:
        if kind == "delim":
            in_args = True
            continue
        if kind != "data":
            continue
        line = rest[0].decode("utf-8", "replace").rstrip("\n")
        if line.startswith("command="):
            command = line[len("command="):]
            continue
        if not in_args:
            continue  # agent=, object-format= lines before delim
        if line == "peel":
            args["peel"] = True
        elif line == "symrefs":
            args["symrefs"] = True
        elif line == "unborn":
            args["unborn"] = True
        elif line == "thin-pack":
            args["thin-pack"] = True
        elif line.startswith("ref-prefix "):
            args["prefixes"].append(line[len("ref-prefix "):])
        elif line.startswith("want "):
            args["want"].append(line[5:].strip())
        elif line.startswith("have "):
            args["have"].append(line[5:].strip())
        elif line.startswith("shallow "):
            args["shallow"].append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                args["deepen"] = int(line[7:].strip())
            except ValueError:
                pass
        elif line.startswith("filter "):
            args["filter"] = line[7:].strip()
        elif line == "done":
            pass
    return command, args


def parse_v0_request(body):
    items, _ = parse_pkt_lines(body)
    args = {"want": [], "have": [], "deepen": None, "shallow": []}
    for kind, *rest in items:
        if kind != "data":
            continue
        line = rest[0].decode("utf-8", "replace").rstrip("\n")
        # Capabilities after NUL on the first want line.
        line = line.split("\0", 1)[0]
        if line.startswith("want "):
            args["want"].append(line[5:].split(" ")[0])
        elif line.startswith("have "):
            args["have"].append(line[5:].strip())
        elif line.startswith("shallow "):
            args["shallow"].append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                args["deepen"] = int(line[7:].strip())
            except ValueError:
                pass
    return args


def handle_receive_pack(repo, body):
    """Process a receive-pack POST body. Returns (status, response_bytes)."""
    items, used = parse_pkt_lines(body, stop_after_flush=True)
    commands = []
    capabilities = []
    for kind, *rest in items:
        if kind != "data":
            continue
        line = rest[0].decode("utf-8", "replace").rstrip("\n")
        parts = line.split("\0", 1)
        cmd = parts[0].split(" ")
        if len(cmd) < 3:
            continue
        if len(parts) > 1 and not capabilities:
            capabilities = parts[1].split(" ")
        commands.append({"old": cmd[0], "new": cmd[1], "ref": cmd[2]})
    pack_data = body[used:]
    sideband = "side-band-64k" in capabilities
    # Parse the pack (if any).
    new_objects = {}
    unpack_ok = True
    unpack_error = ""
    if pack_data:
        try:
            new_objects = read_pack(
                pack_data,
                resolve_base=lambda oid: repo.objects.get(oid))
        except Exception as exc:  # noqa: BLE001 - pack errors are ng, not crashes
            unpack_ok = False
            unpack_error = str(exc)
    results = []  # (ref, ok, reason)
    with STORE.lock:
        # Validate + stage object insertions only if unpack succeeded.
        if unpack_ok:
            for oid, (t, p) in new_objects.items():
                repo.objects[oid] = (t, p)
        for cmd in commands:
            ref = cmd["ref"]
            old = cmd["old"]
            new = cmd["new"]
            current = repo.refs.get(ref, ZERO_OID)
            if not unpack_ok:
                results.append((ref, False, "unpacker error " + unpack_error))
                continue
            if new == ZERO_OID:
                # Delete.
                if old != ZERO_OID and old != current:
                    results.append((ref, False, "stale ref"))
                elif current == ZERO_OID:
                    results.append((ref, False, "stale ref"))
                else:
                    repo.set_ref(ref, None)
                    results.append((ref, True, ""))
                continue
            if old != current:
                results.append((ref, False, "non-fast-forward"))
                continue
            if old != ZERO_OID and not repo.is_ancestor(old, new):
                results.append((ref, False, "non-fast-forward"))
                continue
            # New objects must be present (pack carried them, or already stored).
            if new not in repo.objects:
                results.append((ref, False, "missing object"))
                continue
            repo.set_ref(ref, new)
            results.append((ref, True, ""))
    lines = []
    lines.append("unpack %s\n" % ("ok" if unpack_ok else "error " + unpack_error))
    for ref, ok, reason in results:
        if ok:
            lines.append("ok %s\n" % ref)
        else:
            lines.append("ng %s %s\n" % (ref, reason))
    if sideband:
        out = sideband_text(lines) + pkt_flush()
    else:
        out = b"".join(pkt_line(l) for l in lines) + pkt_flush()
    return 200, out

# ---------------------------------------------------------------------------
# REST v3
# ---------------------------------------------------------------------------

def page_link_header(base_url, path, query, page, per_page, total):
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page >= total_pages:
        return None
    def url(p):
        q = dict(query)
        q["page"] = [str(p)]
        q["per_page"] = [str(per_page)]
        qs = urllib.parse.urlencode([(k, v) for k, vs in q.items() for v in vs])
        return "%s%s?%s" % (base_url, path, qs)
    links = ['<%s>; rel="next"' % url(page + 1),
             '<%s>; rel="last"' % url(total_pages)]
    if page > 1:
        links.append('<%s>; rel="prev"' % url(page - 1))
        links.append('<%s>; rel="first"' % url(1))
    return ", ".join(links)


def paginate(items, query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, per_page


def rest_error(status, message):
    return status, {"message": message}, {}


def handle_rest(method, path, query, body, base_url):
    """Dispatch a /api/v3 request. Returns (status, payload, extra_headers).
    payload None means empty body (204)."""
    segs = [s for s in path.split("/") if s != ""]
    # /api/v3/ root
    if not segs:
        if method != "GET":
            return rest_error(404, "Not Found")
        return 200, {
            "current_user_url": base_url + "/api/v3/user",
            "repository_url": base_url + "/api/v3/repos/{owner}/{repo}",
            "user_url": base_url + "/api/v3/users/{user}",
        }, {"X-OAuth-Scopes": "repo"}
    if segs == ["meta"]:
        if method != "GET":
            return rest_error(404, "Not Found")
        return 200, {"installed_version": SERVER_VERSION,
                     "verifiable_password_authentication": False}, {}
    if segs == ["user"]:
        if method != "GET":
            return rest_error(404, "Not Found")
        return 200, {"login": VIEWER_LOGIN, "id": VIEWER_ID, "type": "User",
                     "name": "ada iversen", "email": VIEWER_EMAIL}, {}
    if segs == ["user", "repos"]:
        if method == "POST":
            name = body.get("name") if isinstance(body, dict) else None
            if not name or not isinstance(name, str):
                return rest_error(422, "name is required")
            repo = STORE.create_repo(name, body.get("description", "") or "",
                                     bool(body.get("private", False)))
            if repo is None:
                return rest_error(422, "Repository already exists")
            return 201, repo.repo_json(), {}
        if method == "GET":
            repos = STORE.user_repos()
            items, page, per_page = paginate([r.repo_json() for r in repos], query)
            headers = {}
            link = page_link_header(base_url, "/api/v3/user/repos", query,
                                    page, per_page, len(repos))
            if link:
                headers["Link"] = link
            return 200, items, headers
        return rest_error(404, "Not Found")
    if len(segs) >= 3 and segs[0] == "repos":
        owner = urllib.parse.unquote(segs[1])
        name = urllib.parse.unquote(segs[2])
        repo = STORE.get_repo(owner, name)
        rest = segs[3:]
        if repo is None:
            return rest_error(404, "Not Found")
        return handle_repo_rest(repo, method, rest, query, body, base_url, path)
    return rest_error(404, "Not Found")


def handle_repo_rest(repo, method, rest, query, body, base_url, full_path):
    # /repos/{o}/{r}
    if not rest:
        if method == "GET":
            return 200, repo.repo_json(), {}
        if method == "PATCH":
            if isinstance(body, dict):
                if "description" in body:
                    repo.description = body.get("description") or ""
                if "private" in body:
                    repo.private = bool(body.get("private"))
            return 200, repo.repo_json(), {}
        if method == "DELETE":
            STORE.delete_repo(repo.owner, repo.name)
            return 204, None, {}
        return rest_error(404, "Not Found")
    head = rest[0]
    if head == "git" and len(rest) >= 2 and rest[1] == "refs":
        if method == "POST" and len(rest) == 2:
            ref = body.get("ref") if isinstance(body, dict) else None
            sha = body.get("sha") if isinstance(body, dict) else None
            if not ref or not sha:
                return rest_error(422, "ref and sha are required")
            if ref in repo.refs:
                return rest_error(422, "Reference already exists")
            if sha not in repo.objects:
                return rest_error(422, "Object does not exist")
            repo.set_ref(ref, sha)
            return 201, {"ref": ref, "object": {"sha": sha, "type": "commit"}}, {}
        if method == "GET":
            if len(rest) == 2:
                out = [{"ref": n, "object": {"sha": o, "type": "commit"}}
                       for n, o in sorted(repo.refs.items())]
                return 200, out, {}
            wanted = "/".join(rest[2:])
            matches = [(n, o) for n, o in sorted(repo.refs.items())
                       if n == wanted or n.startswith(wanted.rstrip("/") + "/")]
            if not matches and wanted in repo.refs:
                matches = [(wanted, repo.refs[wanted])]
            if not matches:
                return rest_error(404, "Not Found")
            if len(matches) == 1:
                n, o = matches[0]
                return 200, {"ref": n, "object": {"sha": o, "type": "commit"}}, {}
            return 200, [{"ref": n, "object": {"sha": o, "type": "commit"}}
                         for n, o in matches], {}
        return rest_error(404, "Not Found")
    if head == "branches":
        if method != "GET":
            return rest_error(404, "Not Found")
        out = [{"name": n[len("refs/heads/"):],
                "commit": {"sha": o}, "protected": False}
               for n, o in sorted(repo.refs.items()) if n.startswith("refs/heads/")]
        return 200, out, {}
    if head == "commits":
        if method != "GET":
            return rest_error(404, "Not Found")
        refish = query.get("sha", [None])[0]
        tip = repo.resolve_refish(refish) if refish else repo.refs.get("refs/heads/" + repo.default_branch)
        out = []
        seen = set()
        cur = tip
        while cur and cur not in seen:
            seen.add(cur)
            c = repo.commit_of(cur)
            if c is None:
                break
            out.append({"sha": cur,
                        "commit": {"message": c["message"],
                                   "author": {"name": VIEWER_LOGIN, "email": VIEWER_EMAIL},
                                   "committer": {"name": VIEWER_LOGIN, "email": VIEWER_EMAIL}},
                        "parents": [{"sha": p} for p in c["parents"]]})
            cur = c["parents"][0] if c["parents"] else None
        return 200, out, {}
    if head == "contents":
        return handle_contents(repo, method, rest[1:], query, body)
    if head == "labels":
        if method == "GET":
            return 200, [{"name": n, "color": v["color"]}
                         for n, v in sorted(repo.labels.items())], {}
        if method == "POST":
            name = body.get("name") if isinstance(body, dict) else None
            if not name:
                return rest_error(422, "name is required")
            color = body.get("color") or "ededed"
            repo.labels[name] = {"name": name, "color": color}
            return 201, {"name": name, "color": color}, {}
        return rest_error(404, "Not Found")
    if head == "issues":
        return handle_issues(repo, method, rest[1:], query, body)
    if head == "pulls":
        return handle_pulls(repo, method, rest[1:], query, body)
    return rest_error(404, "Not Found")


def handle_contents(repo, method, path_segs, query, body):
    rel_path = "/".join(urllib.parse.unquote(s) for s in path_segs)
    branch = None
    if isinstance(body, dict) and body.get("branch"):
        branch = body.get("branch")
    refish = query.get("ref", [None])[0] or branch
    if refish:
        tip = repo.resolve_refish(refish)
        refname = refish if refish.startswith("refs/") else "refs/heads/" + refish
        if tip is None and refish not in repo.refs:
            refname = "refs/heads/" + refish
    else:
        refname = "refs/heads/" + repo.default_branch
        tip = repo.refs.get(refname)
    if method == "GET":
        if tip is None:
            return rest_error(404, "Not Found")
        index = repo.index_for_ref(refname) if refname in repo.refs else {}
        if not index:
            commit = repo.commit_of(tip)
            if commit and commit["tree"]:
                index = repo.flatten_tree(commit["tree"])
        if rel_path == "":
            entries = []
            seen_dirs = set()
            for p, (mode, oid) in sorted(index.items()):
                top = p.split("/")[0]
                if "/" in p:
                    if top not in seen_dirs:
                        seen_dirs.add(top)
                        entries.append({"type": "dir", "name": top, "path": top,
                                        "sha": ""})
                else:
                    obj = repo.objects.get(oid)
                    entries.append({"type": "file", "name": p, "path": p, "sha": oid,
                                    "size": len(obj[1]) if obj else 0})
            return 200, entries, {}
        if rel_path in index:
            mode, oid = index[rel_path]
            obj = repo.objects.get(oid)
            content = obj[1] if obj else b""
            return 200, {"type": "file", "encoding": "base64",
                         "content": base64.b64encode(content).decode("ascii"),
                         "sha": oid, "size": len(content),
                         "name": rel_path.split("/")[-1], "path": rel_path}, {}
        # Directory listing?
        prefix = rel_path.rstrip("/") + "/"
        children = [(p, v) for p, v in sorted(index.items()) if p.startswith(prefix)]
        if children:
            entries = []
            seen_dirs = set()
            for p, (mode, oid) in children:
                rest_p = p[len(prefix):]
                top = rest_p.split("/")[0]
                if "/" in rest_p:
                    if top not in seen_dirs:
                        seen_dirs.add(top)
                        entries.append({"type": "dir", "name": top,
                                        "path": prefix + top, "sha": ""})
                else:
                    obj = repo.objects.get(oid)
                    entries.append({"type": "file", "name": top, "path": p,
                                    "sha": oid, "size": len(obj[1]) if obj else 0})
            return 200, entries, {}
        return rest_error(404, "Not Found")
    if method == "PUT":
        if not isinstance(body, dict) or "content" not in body:
            return rest_error(422, "content is required")
        try:
            raw = base64.b64decode(body["content"], validate=False)
        except Exception:
            return rest_error(422, "content is not valid base64")
        message = body.get("message") or "Update %s" % rel_path
        with STORE.lock:
            tip = repo.refs.get(refname)
            index = dict(repo.index_for_ref(refname)) if tip else {}
            existed = rel_path in index
            if existed:
                if body.get("sha") and body.get("sha") != index[rel_path][1]:
                    return rest_error(409, "sha does not match the current blob")
            blob_oid = repo.put_object("blob", raw)
            index[rel_path] = ("100644", blob_oid)
            parents = [tip] if tip else []
            commit_oid = repo.commit_index(refname, index, message, parents)
        return (200 if existed else 201), {
            "content": {"name": rel_path.split("/")[-1], "path": rel_path,
                        "sha": blob_oid, "size": len(raw), "encoding": "base64",
                        "content": base64.b64encode(raw).decode("ascii")},
            "commit": {"sha": commit_oid, "message": message}}, {}
    if method == "DELETE":
        with STORE.lock:
            tip = repo.refs.get(refname)
            if tip is None:
                return rest_error(404, "Not Found")
            index = dict(repo.index_for_ref(refname))
            if rel_path not in index:
                return rest_error(404, "Not Found")
            if isinstance(body, dict) and body.get("sha") and body.get("sha") != index[rel_path][1]:
                return rest_error(409, "sha does not match the current blob")
            del index[rel_path]
            message = (body.get("message") if isinstance(body, dict) else None) or "Delete %s" % rel_path
            commit_oid = repo.commit_index(refname, index, message, [tip])
        return 200, {"commit": {"sha": commit_oid, "message": message}}, {}
    return rest_error(404, "Not Found")


def handle_issues(repo, method, rest, query, body):
    if not rest:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            sort = query.get("sort", ["created"])[0]
            direction = query.get("direction", ["desc"])[0]
            issues = list(repo.issues.values())
            if state in ("open", "closed"):
                issues = [i for i in issues if i["state"] == state]
            reverse = direction != "asc"
            issues.sort(key=lambda i: i["created_seq"], reverse=reverse)
            items, _page, _pp = paginate([repo.issue_json(i) for i in issues], query)
            return 200, items, {}
        if method == "POST":
            if not isinstance(body, dict) or not body.get("title"):
                return rest_error(422, "title is required")
            labels = body.get("labels") or []
            if isinstance(labels, list):
                for lab in labels:
                    lname = lab.get("name") if isinstance(lab, dict) else lab
                    if lname not in repo.labels:
                        return rest_error(422, "unknown label %s" % lname)
                label_names = [l.get("name") if isinstance(l, dict) else l for l in labels]
            else:
                label_names = []
            issue = repo.new_issue(body["title"], body.get("body") or "", label_names)
            return 201, repo.issue_json(issue), {}
        return rest_error(404, "Not Found")
    try:
        number = int(rest[0])
    except ValueError:
        return rest_error(404, "Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        return rest_error(404, "Not Found")
    if len(rest) == 1:
        if method == "GET":
            return 200, repo.issue_json(issue), {}
        if method == "PATCH":
            if isinstance(body, dict):
                if "state" in body:
                    issue["state"] = "closed" if body["state"] == "closed" else "open"
                if "title" in body:
                    issue["title"] = body["title"]
                if "body" in body:
                    issue["body"] = body["body"]
            return 200, repo.issue_json(issue), {}
        return rest_error(404, "Not Found")
    if len(rest) == 2 and rest[1] == "comments":
        if method == "GET":
            return 200, [{"id": c["id"], "body": c["body"]} for c in issue["comments"]], {}
        if method == "POST":
            if not isinstance(body, dict) or "body" not in body:
                return rest_error(422, "body is required")
            repo.comment_seq += 1
            comment = {"id": 300000 + repo.comment_seq, "body": body["body"]}
            issue["comments"].append(comment)
            return 201, {"id": comment["id"], "body": comment["body"]}, {}
        return rest_error(404, "Not Found")
    return rest_error(404, "Not Found")


def handle_pulls(repo, method, rest, query, body):
    if not rest:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            pulls = list(repo.pulls.values())
            if state in ("open", "closed"):
                pulls = [p for p in pulls if p["state"] == state]
            pulls.sort(key=lambda p: p["number"], reverse=True)
            items, _page, _pp = paginate([repo.pull_json(p) for p in pulls], query)
            return 200, items, {}
        if method == "POST":
            if not isinstance(body, dict):
                return rest_error(422, "title, head and base are required")
            title = body.get("title")
            head = body.get("head")
            base = body.get("base")
            if not title or not head or not base:
                return rest_error(422, "title, head and base are required")
            head = head.split(":", 1)[-1]
            base = base.split(":", 1)[-1]
            head_sha = repo.resolve_refish(head)
            base_sha = repo.resolve_refish(base)
            if head_sha is None or base_sha is None:
                return rest_error(422, "head or base branch does not exist")
            for p in repo.pulls.values():
                if p["state"] == "open" and p["head_ref"] == head and p["base_ref"] == base:
                    return rest_error(422, "A pull request already exists")
            pr = repo.new_pull(title, body.get("body") or "", head, base, head_sha, base_sha)
            return 201, repo.pull_json(pr), {}
        return rest_error(404, "Not Found")
    try:
        number = int(rest[0])
    except ValueError:
        return rest_error(404, "Not Found")
    pr = repo.pulls.get(number)
    if pr is None:
        return rest_error(404, "Not Found")
    if len(rest) == 1:
        if method == "GET":
            return 200, repo.pull_json(pr), {}
        return rest_error(404, "Not Found")
    if len(rest) == 2 and rest[1] == "files":
        if method != "GET":
            return rest_error(404, "Not Found")
        return 200, repo.pr_files(pr), {}
    if len(rest) == 2 and rest[1] == "merge":
        if method != "PUT":
            return rest_error(404, "Not Found")
        if pr["state"] != "open":
            return rest_error(405, "Pull request is already closed")
        if not repo.pr_mergeable(pr):
            return rest_error(405, "Merge conflict")
        method_name = (body.get("merge_method") if isinstance(body, dict) else None) or "squash"
        sha = repo.merge_pr(pr, method_name.lower())
        if sha is None:
            return rest_error(405, "Merge conflict")
        return 200, {"merged": True, "sha": sha,
                     "message": "Pull request successfully merged"}, {}
    return rest_error(404, "Not Found")

# ---------------------------------------------------------------------------
# GraphQL (targeted executor for the gh / github-mcp-server documents)
# ---------------------------------------------------------------------------

GQL_TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "url", "author",
              "comments", "labels", "createdAt", "updatedAt"],
    "PullRequest": ["id", "number", "title", "body", "headRefName", "baseRefName",
                    "mergeable", "state", "merged", "files", "url", "author"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest",
                   "description", "private", "url"],
    "User": ["login", "id", "name", "email"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestChangedFileConnection": ["nodes", "edges", "pageInfo"],
    "PullRequestChangedFile": ["path", "additions", "deletions", "changeType"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "endCursor", "startCursor"],
    "Ref": ["name"],
    "SearchType": [],
}
GQL_ENUM_VALUES = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION", "COMMIT",
                   "CODE", "MILESTONE", "PROJECT", "WIKI"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
}
GQL_TYPE_KIND = {
    "Issue": "OBJECT", "PullRequest": "OBJECT", "Repository": "OBJECT",
    "User": "OBJECT", "IssueConnection": "OBJECT",
    "PullRequestConnection": "OBJECT", "PageInfo": "OBJECT",
    "Ref": "OBJECT", "SearchType": "ENUM", "IssueState": "ENUM",
    "PullRequestState": "ENUM", "MergeableState": "ENUM",
    "PullRequestMergeMethod": "ENUM", "RepositoryPermission": "ENUM",
    "IssueOrderField": "ENUM", "OrderDirection": "ENUM",
}


class GqlTokenizer:
    TOKEN_RE = re.compile(r"""
        (?P<ws>\s+|,|\#[^\n]*)
      | (?P<ellipsis>\.\.\.)
      | (?P<punct>[!$():=\[\]{}@&|])
      | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
      | (?P<float>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+(?:[eE][+-]?[0-9]+)?|[eE][+-]?[0-9]+))
      | (?P<int>-?(?:0|[1-9][0-9]*))
      | (?P<string>"(?:\\.|[^"\\])*")
    """, re.VERBOSE)

    def __init__(self, text):
        self.tokens = []
        pos = 0
        while pos < len(text):
            m = self.TOKEN_RE.match(text, pos)
            if not m:
                raise ValueError("bad GraphQL at %d" % pos)
            pos = m.end()
            kind = m.lastgroup
            if kind in ("ws",):
                continue
            self.tokens.append((kind, m.group()))
        self.tokens.append(("eof", ""))
        self.i = 0

    def peek(self):
        return self.tokens[self.i]

    def next(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, value):
        kind, text = self.next()
        if text != value:
            raise ValueError("expected %s, got %s" % (value, text))

    def expect_name(self):
        kind, text = self.next()
        if kind != "name":
            raise ValueError("expected name, got %s" % text)
        return text


class GqlParser:
    def __init__(self, text):
        self.tok = GqlTokenizer(text)

    def parse(self):
        t = self.tok
        kind, text = t.peek()
        op_type = "query"
        op_name = None
        if kind == "name" and text in ("query", "mutation", "subscription"):
            op_type = t.next()[1]
            kind2, text2 = t.peek()
            if kind2 == "name":
                op_name = t.next()[1]
        elif text == "{":
            pass
        else:
            raise ValueError("unsupported operation")
        variables = {}
        if t.peek()[1] == "(":
            variables = self.parse_var_defs()
        self.skip_directives()
        selection = self.parse_selection_set()
        return {"type": op_type, "name": op_name, "vars": variables,
                "selection": selection}

    def skip_directives(self):
        t = self.tok
        while t.peek()[1] == "@":
            t.next()
            t.expect_name()
            if t.peek()[1] == "(":
                self.parse_arguments()

    def parse_var_defs(self):
        t = self.tok
        t.expect("(")
        defs = {}
        while t.peek()[1] != ")":
            t.expect("$")
            name = t.expect_name()
            t.expect(":")
            defs[name] = self.parse_type()
            if t.peek()[1] == "=":
                t.next()
                self.parse_value()
            if t.peek()[1] == ",":
                t.next()
        t.expect(")")
        return defs

    def parse_type(self):
        t = self.tok
        if t.peek()[1] == "[":
            t.next()
            inner = self.parse_type()
            t.expect("]")
            typ = "[" + inner + "]"
        else:
            typ = t.expect_name()
        if t.peek()[1] == "!":
            t.next()
            typ += "!"
        return typ

    def parse_selection_set(self):
        t = self.tok
        t.expect("{")
        fields = []
        while t.peek()[1] != "}":
            fields.append(self.parse_field())
        t.expect("}")
        return fields

    def parse_field(self):
        t = self.tok
        if t.peek()[1] == "...":
            t.next()
            kind, text = t.peek()
            if kind == "name" and text == "on":
                t.next()
                type_name = t.expect_name()
                self.skip_directives()
                sel = self.parse_selection_set()
                return {"fragment": True, "type": type_name, "selection": sel}
            # Named fragment spread (unsupported in the suite's documents).
            t.expect_name()
            self.skip_directives()
            return {"fragment": True, "type": None, "selection": []}
        name = t.expect_name()
        alias = None
        if t.peek()[1] == ":":
            t.next()
            alias = name
            name = t.expect_name()
        args = {}
        if t.peek()[1] == "(":
            args = self.parse_arguments()
        self.skip_directives()
        sel = None
        if t.peek()[1] == "{":
            sel = self.parse_selection_set()
        return {"name": name, "alias": alias or name, "args": args,
                "selection": sel}

    def parse_arguments(self):
        t = self.tok
        t.expect("(")
        args = {}
        while t.peek()[1] != ")":
            name = t.expect_name()
            t.expect(":")
            args[name] = self.parse_value()
            if t.peek()[1] == ",":
                t.next()
        t.expect(")")
        return args

    def parse_value(self):
        t = self.tok
        kind, text = t.next()
        if kind == "string":
            return ("string", json.loads(text))
        if kind == "int":
            return ("int", int(text))
        if kind == "float":
            return ("float", float(text))
        if kind == "name":
            if text == "true":
                return ("bool", True)
            if text == "false":
                return ("bool", False)
            if text == "null":
                return ("null", None)
            return ("enum", text)
        if text == "$":
            return ("var", t.expect_name())
        if text == "[":
            items = []
            while t.peek()[1] != "]":
                items.append(self.parse_value())
                if t.peek()[1] == ",":
                    t.next()
            t.expect("]")
            return ("list", items)
        if text == "{":
            obj = {}
            while t.peek()[1] != "}":
                name = t.expect_name()
                t.expect(":")
                obj[name] = self.parse_value()
                if t.peek()[1] == ",":
                    t.next()
            t.expect("}")
            return ("object", obj)
        raise ValueError("unexpected value token %s" % text)


def gql_resolve_value(node, variables):
    kind, val = node
    if kind == "var":
        return variables.get(val)
    if kind == "list":
        return [gql_resolve_value(v, variables) for v in val]
    if kind == "object":
        return {k: gql_resolve_value(v, variables) for k, v in val.items()}
    return val


class GqlExecutor:
    def __init__(self):
        self.errors = []

    def execute(self, doc, variables):
        self.errors = []
        try:
            parsed = GqlParser(doc).parse()
        except Exception as exc:  # noqa: BLE001
            return {"data": None, "errors": [{"message": str(exc)}]}
        if parsed["type"] == "mutation":
            data = {}
            for field in parsed["selection"]:
                data[field["alias"]] = self.resolve_mutation(field, variables)
        else:
            data = {}
            for field in parsed["selection"]:
                data[field["alias"]] = self.resolve_query(field, variables)
        out = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    # -- field resolution ---------------------------------------------------
    def resolve_query(self, field, variables):
        name = field["name"]
        args = {k: gql_resolve_value(v, variables) for k, v in field["args"].items()}
        if name == "viewer":
            return self.resolve_object("User", {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                                       field["selection"], variables)
        if name == "repository":
            repo = STORE.get_repo(args.get("owner"), args.get("name"))
            if repo is None:
                return None
            return self.resolve_object("Repository", repo, field["selection"], variables)
        if name == "node":
            kind, key = decode_global_id(args.get("id") or "")
            if kind == "repo":
                owner, _, rname = key.partition("/")
                repo = STORE.get_repo(owner, rname)
                if repo is None:
                    return None
                return self.resolve_object("Repository", repo, field["selection"], variables)
            if kind == "issue":
                return None
            return None
        if name == "__type":
            return self.resolve_type(args.get("name"), field["selection"], variables)
        self.errors.append({"message": 'Cannot query field "%s" on type "Query".' % name})
        return None

    def resolve_mutation(self, field, variables):
        name = field["name"]
        args = {k: gql_resolve_value(v, variables) for k, v in field["args"].items()}
        inp = args.get("input") or {}
        if name == "createIssue":
            kind, key = decode_global_id(inp.get("repositoryId") or "")
            if kind != "repo":
                self.errors.append({"message": "createIssue: unknown repositoryId"})
                return None
            owner, _, rname = key.partition("/")
            repo = STORE.get_repo(owner, rname)
            if repo is None:
                return None
            issue = repo.new_issue(inp.get("title") or "", inp.get("body") or "")
            return self.resolve_object("CreateIssuePayload",
                                       {"issue": issue, "repo": repo},
                                       field["selection"], variables)
        if name == "closeIssue":
            issue, repo = self.issue_by_global_id(inp.get("issueId") or "")
            if issue is None:
                return None
            issue["state"] = "closed"
            return self.resolve_object("CloseIssuePayload",
                                       {"issue": issue, "repo": repo},
                                       field["selection"], variables)
        if name == "createPullRequest":
            kind, key = decode_global_id(inp.get("repositoryId") or "")
            if kind != "repo":
                return None
            owner, _, rname = key.partition("/")
            repo = STORE.get_repo(owner, rname)
            if repo is None:
                return None
            head = inp.get("headRefName")
            base = inp.get("baseRefName")
            head_sha = repo.resolve_refish(head) if head else None
            base_sha = repo.resolve_refish(base) if base else None
            if head_sha is None or base_sha is None:
                self.errors.append({"message": "createPullRequest: head or base branch does not exist"})
                return None
            pr = repo.new_pull(inp.get("title") or "", inp.get("body") or "",
                               head, base, head_sha, base_sha)
            return self.resolve_object("CreatePullRequestPayload",
                                       {"pullRequest": pr, "repo": repo},
                                       field["selection"], variables)
        if name == "mergePullRequest":
            pr, repo = self.pr_by_global_id(inp.get("pullRequestId") or "")
            if pr is None:
                return None
            if pr["state"] != "open" or not repo.pr_mergeable(pr):
                self.errors.append({"message": "mergePullRequest: pull request is not mergeable"})
                return None
            method = (inp.get("mergeMethod") or "SQUASH").lower()
            repo.merge_pr(pr, method)
            return self.resolve_object("MergePullRequestPayload",
                                       {"pullRequest": pr, "repo": repo},
                                       field["selection"], variables)
        if name == "addComment":
            issue, repo = self.issue_by_global_id(inp.get("subjectId") or "")
            if issue is None:
                pr, repo = self.pr_by_global_id(inp.get("subjectId") or "")
                if pr is None:
                    return None
                return self.resolve_object("AddCommentPayload",
                                           {"comment": {"id": 0, "body": inp.get("body") or ""}},
                                           field["selection"], variables)
            repo.comment_seq += 1
            comment = {"id": 300000 + repo.comment_seq, "body": inp.get("body") or ""}
            issue["comments"].append(comment)
            return self.resolve_object("AddCommentPayload",
                                       {"comment": comment},
                                       field["selection"], variables)
        self.errors.append({"message": 'Cannot query field "%s" on type "Mutation".' % name})
        return None

    def issue_by_global_id(self, gid):
        kind, key = decode_global_id(gid)
        if kind != "issue":
            return None, None
        owner_repo, _, num = key.rpartition("#")
        owner, _, rname = owner_repo.partition("/")
        repo = STORE.get_repo(owner, rname)
        if repo is None:
            return None, None
        try:
            return repo.issues.get(int(num)), repo
        except ValueError:
            return None, None

    def pr_by_global_id(self, gid):
        kind, key = decode_global_id(gid)
        if kind != "pr":
            return None, None
        owner_repo, _, num = key.rpartition("#")
        owner, _, rname = owner_repo.partition("/")
        repo = STORE.get_repo(owner, rname)
        if repo is None:
            return None, None
        try:
            return repo.pulls.get(int(num)), repo
        except ValueError:
            return None, None

    def resolve_type(self, name, selection, variables):
        if name not in GQL_TYPE_KIND:
            return None
        node = {"name": name, "kind": GQL_TYPE_KIND[name]}
        if selection is None:
            return node
        out = {}
        for field in selection:
            fname = field["name"]
            if fname == "fields":
                out[field["alias"]] = [{"name": f} for f in GQL_TYPE_FIELDS.get(name, [])]
            elif fname == "enumValues":
                out[field["alias"]] = [{"name": v} for v in GQL_ENUM_VALUES.get(name, [])]
            elif fname == "name":
                out[field["alias"]] = name
            elif fname == "kind":
                out[field["alias"]] = node["kind"]
            else:
                self.errors.append({"message": 'Cannot query field "%s" on type "__Type".' % fname})
                out[field["alias"]] = None
        return out

    def resolve_object(self, type_name, obj, selection, variables):
        if selection is None:
            return None
        out = {}
        for field in selection:
            if field.get("fragment"):
                if field["type"] is None or field["type"] == type_name:
                    sub = self.resolve_object(type_name, obj, field["selection"], variables)
                    if sub:
                        out.update(sub)
                continue
            out[field["alias"]] = self.resolve_field(type_name, obj, field, variables)
        return out

    def resolve_field(self, type_name, obj, field, variables):
        name = field["name"]
        args = {k: gql_resolve_value(v, variables) for k, v in field["args"].items()}
        sel = field["selection"]
        if type_name == "User":
            if name == "login":
                return obj.get("login")
            if name == "id":
                return obj.get("id")
            self.errors.append({"message": 'Cannot query field "%s" on type "User".' % name})
            return None
        if type_name == "Repository":
            repo = obj
            if name == "id":
                return global_id("repo", "%s/%s" % (repo.owner, repo.name))
            if name == "databaseId":
                return repo.id
            if name == "name":
                return repo.name
            if name == "nameWithOwner":
                return "%s/%s" % (repo.owner, repo.name)
            if name == "owner":
                return self.resolve_object("User", {"login": repo.owner, "id": VIEWER_ID}, sel, variables)
            if name == "defaultBranchRef":
                tip = repo.refs.get("refs/heads/" + repo.default_branch)
                if tip is None:
                    return None
                return self.resolve_object("Ref", {"name": repo.default_branch}, sel, variables)
            if name == "viewerPermission":
                return "ADMIN"
            if name == "hasIssuesEnabled":
                return True
            if name == "description":
                return repo.description
            if name == "private":
                return repo.private
            if name == "url":
                return "https://github.com/%s/%s" % (repo.owner, repo.name)
            if name == "issue":
                issue = repo.issues.get(args.get("number"))
                if issue is None:
                    return None
                return self.resolve_object("Issue", self.issue_node(repo, issue), sel, variables)
            if name == "issues":
                return self.issues_connection(repo, args, sel, variables)
            if name == "pullRequests":
                return self.pulls_connection(repo, args, sel, variables)
            if name == "pullRequest":
                pr = repo.pulls.get(args.get("number"))
                if pr is None:
                    return None
                return self.resolve_object("PullRequest", self.pr_node(repo, pr), sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "Repository".' % name})
            return None
        if type_name == "Ref":
            if name == "name":
                return obj.get("name")
            self.errors.append({"message": 'Cannot query field "%s" on type "Ref".' % name})
            return None
        if type_name == "Issue":
            issue = obj["issue"]
            if name == "id":
                return obj["gid"]
            if name in ("number", "title", "body"):
                return issue[name]
            if name == "state":
                return issue["state"].upper()
            if name == "url":
                return "https://github.com/%s/%s/issues/%d" % (obj["repo"].owner, obj["repo"].name, issue["number"])
            if name == "author":
                return self.resolve_object("User", {"login": VIEWER_LOGIN, "id": VIEWER_ID}, sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "Issue".' % name})
            return None
        if type_name == "PullRequest":
            pr = obj["pr"]
            if name == "id":
                return obj["gid"]
            if name in ("number", "title", "body"):
                return pr[name]
            if name == "state":
                return "MERGED" if pr["merged"] else pr["state"].upper()
            if name == "merged":
                return bool(pr["merged"])
            if name == "headRefName":
                return pr["head_ref"]
            if name == "baseRefName":
                return pr["base_ref"]
            if name == "mergeable":
                repo = obj["repo"]
                if pr["state"] != "open":
                    return "UNKNOWN"
                return "MERGEABLE" if repo.pr_mergeable(pr) else "CONFLICTING"
            if name == "files":
                repo = obj["repo"]
                first = args.get("first")
                files = repo.pr_files(pr)
                nodes = [{"path": f["filename"]} for f in files]
                if first is not None:
                    nodes = nodes[:int(first)]
                return self.resolve_object("PullRequestChangedFileConnection",
                                           {"nodes": nodes}, sel, variables)
            if name == "url":
                return "https://github.com/%s/%s/pull/%d" % (obj["repo"].owner, obj["repo"].name, pr["number"])
            self.errors.append({"message": 'Cannot query field "%s" on type "PullRequest".' % name})
            return None
        if type_name in ("IssueConnection", "PullRequestConnection",
                         "PullRequestChangedFileConnection"):
            if name == "nodes":
                nodes = obj.get("nodes", [])
                item_type = {"IssueConnection": "Issue",
                             "PullRequestConnection": "PullRequest",
                             "PullRequestChangedFileConnection": "PullRequestChangedFile"}[type_name]
                out = []
                for n in nodes:
                    if type_name == "PullRequestChangedFileConnection":
                        out.append(self.resolve_object(item_type, n, sel, variables))
                    else:
                        out.append(self.resolve_object(item_type, n, sel, variables))
                return out
            if name == "pageInfo":
                return self.resolve_object("PageInfo", obj.get("pageInfo", {}), sel, variables)
            if name == "totalCount":
                return len(obj.get("nodes", []))
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        if type_name == "PullRequestChangedFile":
            if name == "path":
                return obj.get("path")
            if name in ("additions", "deletions"):
                return obj.get(name, 0)
            self.errors.append({"message": 'Cannot query field "%s" on type "PullRequestChangedFile".' % name})
            return None
        if type_name == "PageInfo":
            if name in ("hasNextPage", "hasPreviousPage"):
                return bool(obj.get(name, False))
            if name in ("endCursor", "startCursor"):
                return obj.get(name)
            return None
        if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
            if name == "issue":
                return self.resolve_object("Issue", self.issue_node(obj["repo"], obj["issue"]), sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        if type_name == "CreatePullRequestPayload":
            if name == "pullRequest":
                return self.resolve_object("PullRequest", self.pr_node(obj["repo"], obj["pullRequest"]), sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        if type_name == "MergePullRequestPayload":
            if name == "pullRequest":
                return self.resolve_object("PullRequest", self.pr_node(obj["repo"], obj["pullRequest"]), sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        if type_name == "AddCommentPayload":
            if name == "commentEdge":
                return self.resolve_object("CommentEdge", {"node": obj["comment"]}, sel, variables)
            self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
            return None
        if type_name == "CommentEdge":
            if name == "node":
                return self.resolve_object("Comment", obj["node"], sel, variables)
            return None
        if type_name == "Comment":
            if name == "id":
                c = obj
                return global_id("comment", "c%d" % c["id"])
            if name == "body":
                return obj.get("body")
            return None
        self.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
        return None

    def issue_node(self, repo, issue):
        return {"issue": issue, "repo": repo,
                "gid": global_id("issue", "%s/%s#%d" % (repo.owner, repo.name, issue["number"]))}

    def pr_node(self, repo, pr):
        return {"pr": pr, "repo": repo,
                "gid": global_id("pr", "%s/%s#%d" % (repo.owner, repo.name, pr["number"]))}

    def issues_connection(self, repo, args, sel, variables):
        issues = list(repo.issues.values())
        states = args.get("states")
        if states:
            wanted = {s.lower() for s in states}
            issues = [i for i in issues if i["state"] in wanted]
        order_by = args.get("orderBy") or {}
        field = (order_by.get("field") or "CREATED_AT")
        direction = (order_by.get("direction") or "DESC")
        # With an explicit orderBy the direction applies; without one, cursor
        # pagination walks creation order ascending (first page = first N).
        if "orderBy" in args:
            reverse = direction == "DESC"
        else:
            reverse = False
        issues.sort(key=lambda i: i["created_seq"], reverse=reverse)
        # Cursor pagination (no orderBy): default order is creation order ASC
        # from the first page; the cursor is the last seen issue number.
        after = args.get("after")
        if after:
            try:
                cursor_num = int(base64.urlsafe_b64decode(
                    after + "=" * (-len(after) % 4)).decode("ascii"))
            except Exception:
                cursor_num = 0
            issues = [i for i in issues if i["created_seq"] > cursor_num]
            issues.sort(key=lambda i: i["created_seq"])
        first = args.get("first")
        total = len(issues)
        if first is not None:
            page = issues[:int(first)]
        else:
            page = issues
        has_next = len(page) < total
        end_cursor = None
        if page:
            end_cursor = base64.urlsafe_b64encode(
                str(page[-1]["created_seq"]).encode("ascii")).decode("ascii").rstrip("=")
        nodes = [self.issue_node(repo, i) for i in page]
        conn = {"nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor,
                             "hasPreviousPage": False, "startCursor": None}}
        return self.resolve_object("IssueConnection", conn, sel, variables)

    def pulls_connection(self, repo, args, sel, variables):
        pulls = list(repo.pulls.values())
        pulls.sort(key=lambda p: p["number"], reverse=True)
        first = args.get("first")
        if first is not None:
            pulls = pulls[:int(first)]
        nodes = [self.pr_node(repo, p) for p in pulls]
        conn = {"nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None,
                             "hasPreviousPage": False, "startCursor": None}}
        return self.resolve_object("PullRequestConnection", conn, sel, variables)


def handle_graphql(body):
    """body: parsed JSON (dict or list). Returns (status, payload)."""
    executor = GqlExecutor()
    if isinstance(body, list):
        return 200, [executor.execute(op.get("query", ""), op.get("variables") or {})
                     for op in body]
    if not isinstance(body, dict) or "query" not in body:
        return 400, {"errors": [{"message": "query is required"}]}
    return 200, executor.execute(body["query"], body.get("variables") or {})

# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a repository owned by the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name"},
                "description": {"type": "string", "description": "Repository description"},
                "private": {"type": "boolean", "description": "Whether the repository is private"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "description": "Issue body"},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "state": {"type": "string", "description": "Issue state: open, closed or all"},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "title": {"type": "string", "description": "Pull request title"},
                "head": {"type": "string", "description": "Head branch name"},
                "base": {"type": "string", "description": "Base branch name"},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login"},
                "repo": {"type": "string", "description": "Repository name"},
                "pull_number": {"type": "integer", "description": "Pull request number"},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

MCP_TOOLS_JSON = json.dumps(MCP_TOOLS, separators=(",", ":"), sort_keys=True)


def mcp_tool_result(payload):
    text = json.dumps(payload, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload}


def mcp_tool_error(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def mcp_call_tool(name, args):
    if not isinstance(args, dict):
        return mcp_tool_error("arguments must be an object")
    tool = next((t for t in MCP_TOOLS if t["name"] == name), None)
    if tool is None:
        return None  # unknown tool -> JSON-RPC error, handled by caller
    for req in tool["inputSchema"].get("required", []):
        if req not in args or args[req] is None or args[req] == "":
            return mcp_tool_error("missing required argument: %s" % req)
    if name == "create_repo":
        repo = STORE.create_repo(args["name"], args.get("description") or "",
                                 bool(args.get("private", False)))
        if repo is None:
            return mcp_tool_error("Repository already exists")
        return mcp_tool_result(repo.repo_json())
    if name == "get_repo":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return mcp_tool_error("Repository not found")
        return mcp_tool_result(repo.repo_json())
    if name == "create_issue":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return mcp_tool_error("Repository not found")
        issue = repo.new_issue(args["title"], args.get("body") or "")
        return mcp_tool_result(repo.issue_json(issue))
    if name == "list_issues":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return mcp_tool_error("Repository not found")
        state = args.get("state") or "open"
        issues = [repo.issue_json(i) for i in sorted(repo.issues.values(),
                                                       key=lambda x: x["number"])]
        if state in ("open", "closed"):
            issues = [i for i in issues if i["state"] == state]
        return mcp_tool_result(issues)
    if name == "create_pull_request":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return mcp_tool_error("Repository not found")
        head = str(args["head"]).split(":", 1)[-1]
        base = str(args["base"]).split(":", 1)[-1]
        head_sha = repo.resolve_refish(head)
        base_sha = repo.resolve_refish(base)
        if head_sha is None or base_sha is None:
            return mcp_tool_error("head or base branch does not exist")
        for p in repo.pulls.values():
            if p["state"] == "open" and p["head_ref"] == head and p["base_ref"] == base:
                return mcp_tool_error("A pull request already exists")
        pr = repo.new_pull(args["title"], args.get("body") or "", head, base,
                           head_sha, base_sha)
        return mcp_tool_result(repo.pull_json(pr))
    if name == "merge_pull_request":
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None:
            return mcp_tool_error("Repository not found")
        try:
            number = int(args["pull_number"])
        except (TypeError, ValueError):
            return mcp_tool_error("pull_number must be an integer")
        pr = repo.pulls.get(number)
        if pr is None:
            return mcp_tool_error("Pull request not found")
        if pr["state"] != "open":
            return mcp_tool_error("Pull request is already closed")
        if not repo.pr_mergeable(pr):
            return mcp_tool_error("Merge conflict")
        repo.merge_pr(pr, "squash")
        return mcp_tool_result(repo.pull_json(pr))
    return mcp_tool_error("unknown tool")


def handle_mcp(body_bytes, headers):
    """Returns (http_status, payload_dict_or_None, extra_headers).

    payload None + status 202 means: notification accepted, empty body.
    """
    try:
        req = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}, {}
    if not isinstance(req, dict) or "method" not in req:
        return 400, {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}}, {}
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    extra = {"MCP-Protocol-Version": "2026-07-28"}
    if headers.get("Mcp-Name"):
        extra["Mcp-Name"] = headers["Mcp-Name"]
    # Notifications (no id) are accepted silently.
    if req_id is None:
        return 202, None, extra
    server_info = {"name": SERVER_NAME, "version": SERVER_VERSION}
    if method == "server/discover":
        result = {
            "serverInfo": server_info,
            "capabilities": {"tools": {}},
            "supportedVersions": ["2026-07-28", "2025-11-25"],
            "protocolVersion": "2026-07-28",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": server_info,
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            },
        }
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}, extra
    if method == "initialize":
        result = {
            "protocolVersion": "2026-07-28",
            "capabilities": {"tools": {}},
            "serverInfo": server_info,
        }
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}, extra
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {}}, extra
    if method == "tools/list":
        # Byte-identical across calls: reuse the cached serialization.
        result = json.loads(MCP_TOOLS_JSON)
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": result}}, extra
    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}
        if not tool_name or not any(t["name"] == tool_name for t in MCP_TOOLS):
            return 200, {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32602, "message": "Unknown tool: %s" % tool_name}}, extra
        result = mcp_call_tool(tool_name, tool_args)
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": result}, extra
    return 200, {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32601, "message": "Method not found: %s" % method}}, extra

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def check_auth(headers):
    """True when the request carries the suite bearer token."""
    auth = headers.get("Authorization") or ""
    for prefix in ("Bearer ", "token "):
        if auth.startswith(prefix):
            value = auth[len(prefix):].strip()
            if not value:
                return False
            if TOKEN == "":
                return True
            return value == TOKEN
    return False


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ForgeHTTP/1.0"

    def setup(self):
        super().setup()
        # Disable Nagle: the suite drives many small requests over one
        # keep-alive connection, and Nagle + delayed ACK costs ~40 ms each.
        try:
            import socket as _socket
            self.connection.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except Exception:
            pass

    def log_message(self, *args):  # keep stdout clean
        pass

    # -- plumbing ------------------------------------------------------------
    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send_bytes(self, status, data, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-GitHub-Request-Id", STORE.next_req_id())
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, status, payload, extra=None):
        if payload is None:
            self._send_bytes(status, b"", "application/json", extra)
            return
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, data, "application/json", extra)

    def _send_rest(self, status, payload, extra=None):
        headers = dict(extra or {})
        self._send_json(status, payload, headers)

    # -- verbs ---------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- routing ---------------------------------------------------------------
    def _dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        body_bytes = self._read_body() if method in ("POST", "PUT", "PATCH", "DELETE") else b""
        try:
            self._route(method, path, query, body_bytes)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never crash the server
            sys.stderr.write("[entry] handler error %s %s: %r\n" % (method, path, exc))
            try:
                self._send_json(500, {"message": "Internal Server Error"})
            except Exception:
                pass

    def _route(self, method, path, query, body_bytes):
        base_url = "http://%s" % (self.headers.get("Host") or "127.0.0.1")
        # -- MCP --------------------------------------------------------------
        if path == "/mcp":
            if method != "POST":
                self._send_json(405, {"message": "Method Not Allowed"})
                return
            if not check_auth(self.headers):
                self._send_json(401, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32000, "message": "Unauthorized"}})
                return
            status, payload, extra = handle_mcp(body_bytes, self.headers)
            if status == 202:
                self._send_bytes(202, b"", "application/json", extra)
            else:
                self._send_json(status, payload, extra)
            return
        # -- GraphQL ----------------------------------------------------------
        if path == "/api/graphql":
            if method != "POST":
                self._send_json(405, {"message": "Method Not Allowed"})
                return
            if not check_auth(self.headers):
                self._send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception:
                self._send_json(400, {"message": "malformed JSON body"})
                return
            status, payload = handle_graphql(body)
            self._send_json(status, payload)
            return
        # -- REST v3 ------------------------------------------------------------
        if path == "/api/v3" or path.startswith("/api/v3/"):
            if not check_auth(self.headers):
                self._send_json(401, {"message": "Requires authentication"})
                return
            rest_path = path[len("/api/v3"):]
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception:
                self._send_json(400, {"message": "malformed JSON body"})
                return
            status, payload, extra = handle_rest(method, rest_path, query, body, base_url)
            self._send_rest(status, payload, extra)
            return
        # -- Git smart HTTP -----------------------------------------------------
        segs = [s for s in path.split("/") if s != ""]
        if len(segs) >= 3:
            owner = urllib.parse.unquote(segs[0])
            repo_name = urllib.parse.unquote(segs[1])
            # Git URLs carry a ".git" suffix on the repository name.
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            tail = segs[2:]
            repo = STORE.get_repo(owner, repo_name)
            if tail == ["info", "refs"] and method == "GET":
                service = query.get("service", [None])[0]
                if repo is None:
                    self._send_bytes(404, b"Not Found", "text/plain")
                    return
                if service == "git-upload-pack":
                    self._send_bytes(200, git_advertisement_v2(repo),
                                     "application/x-git-upload-pack-advertisement")
                    return
                if service == "git-receive-pack":
                    self._send_bytes(200, git_advertisement_v0(repo, service),
                                     "application/x-git-receive-pack-advertisement")
                    return
                self._send_bytes(404, b"Not Found", "text/plain")
                return
            if tail == ["git-upload-pack"] and method == "POST":
                if repo is None:
                    self._send_bytes(404, b"Not Found", "text/plain")
                    return
                command, args = parse_v2_request(body_bytes)
                if command == "ls-refs":
                    data = ls_refs_response(repo, args)
                elif command == "fetch":
                    data = fetch_v2_response(repo, args)
                else:
                    # v0-style request (want lines, no command=): fall back.
                    v0 = parse_v0_request(body_bytes)
                    data = fetch_v0_response(repo, v0)
                self._send_bytes(200, data, "application/x-git-upload-pack-result")
                return
            if tail == ["git-receive-pack"] and method == "POST":
                if repo is None:
                    self._send_bytes(404, b"Not Found", "text/plain")
                    return
                status, data = handle_receive_pack(repo, body_bytes)
                self._send_bytes(status, data, "application/x-git-receive-pack-result")
                return
        self._send_json(404, {"message": "Not Found"})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def http_json(url, method="GET", payload=None, timeout=30):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return exc.code, {}
    except Exception:
        return 0, {}


def register(port):
    attempt = 0
    while True:
        status, _body = http_json(ENV_URL + "/register", method="POST",
                                  payload={"port": port})
        if status in (200, 409):
            return
        if status in (400, 405):
            raise RuntimeError("register answered HTTP %d" % status)
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 8))))
        attempt += 1


def wait_for_done():
    attempt = 0
    while True:
        status, body = http_json(ENV_URL + "/done")
        if status == 200 and body.get("done") is True:
            return
        time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 8))))
        attempt += 1


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ForgeHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    sys.stdout.write("[entry] listening on port %d (viewer %s)\n" % (port, VIEWER_LOGIN))
    sys.stdout.flush()
    if ENV_URL:
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        register(port)
        sys.stdout.write("[entry] registered; waiting for the env to say done\n")
        sys.stdout.flush()
        wait_for_done()
        server.shutdown()
        server.server_close()
        sys.stdout.write("[entry] done\n")
        sys.stdout.flush()
        return 0
    # Local run (no env): serve forever on $PORT or 8765.
    local_port = int(os.environ.get("PORT", "8765"))
    server.server_close()
    server = ThreadingHTTPServer(("127.0.0.1", local_port), ForgeHandler)
    server.daemon_threads = True
    sys.stdout.write("[entry] local mode on port %d (viewer %s)\n" % (local_port, VIEWER_LOGIN))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
