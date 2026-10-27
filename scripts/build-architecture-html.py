#!/usr/bin/env python3
"""Build the architecture page: one self-contained HTML file from a graph source.

docs/diagrams/architecture-graph.json is the public source; the default output is
docs/architecture.html. --overlay adds the values of one host (the private edition);
that output must go to a path under .local/. The page makes no network request and
works from file://. Standard library only. See docs/host-configuration.md.

  python3 scripts/build-architecture-html.py            # write docs/architecture.html
  python3 scripts/build-architecture-html.py --check    # exit 1 when the file differs from a build
  python3 scripts/build-architecture-html.py --selftest
  python3 scripts/build-architecture-html.py --overlay .local/diagrams/overlay.json \\
      --out .local/diagrams/architecture-local.html

Exit codes: 0 success, 1 --check difference or self-test failure, 2 invalid input or refused path.
"""
import argparse
import copy
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "diagrams" / "architecture-graph.json"
OUT = ROOT / "docs" / "architecture.html"
PARTS = ("core", "option")

# Layout units in pixels. The layers of a view go left to right (LR) or top to bottom (TB);
# a slot is one place in a layer. The builder keeps the direction that fits CANVAS better.
NODE_H = 50
ROW_H = 72         # LR: distance of two slots
COL_GAP = 110      # LR: space between two layers
LAYER_H = 112      # TB: distance of two layers
SLOT_GAP = 28      # TB: space between two slots
EMPTY_SLOT = 24    # TB: width of a slot with no node
CANVAS = (1030, 800)
MARGIN = 60
FRAME_PAD = 14
FRAME_PAD_TOP = 30
LABEL_CHAR = 7.3   # 12 px monospace, a wide estimate
LINE_MAX = 26      # a longer label wraps to two lines
MIN_W = 120

# Colors of the page. The self-test checks the contrast of each text pair (WCAG AA: 4.5).
PAGE_BG = "#0d1117"
PANEL_BG = "#141a23"
NODE_BG = "#161d29"
TEXT = "#e6edf3"
MUTED = "#a3adbb"

SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
SECRET_TEXT = re.compile(
    r"(?i)(?:\bsk-[a-z0-9_-]{12,}|\bghp_[a-z0-9]{20,}|\bgithub_pat_[a-z0-9_]{20,}"
    r"|\bxox[abpr]-[a-z0-9-]{10,}|-----begin [a-z ]*private key-----|\bbearer\s+[a-z0-9._~+/-]{20,})")
SECRET_ENV_NAME = re.compile(r"KEY|SECRET|PASSWORD|TOKEN|SALT|ROLE_ID")


class GraphError(Exception):
    """The source, the overlay or the output path is not valid."""


# ---------------------------------------------------------------- validation

def sentences(text):
    return len(SENTENCE_END.findall(text.strip()))


def validate(graph, root=None):
    """Raise GraphError with each problem. With root, each source path must exist under it."""
    problems = []

    def need(cond, message):
        if not cond:
            problems.append(message)

    groups = {g["id"] for g in graph.get("groups", [])}
    kinds = {k["id"] for k in graph.get("edge_kinds", [])}
    views = {v["id"]: v for v in graph.get("views", [])}
    nodes = graph.get("nodes", [])
    by_id = {}
    for n in nodes:
        need(n.get("id") and n["id"] not in by_id, f"node id missing or repeated: {n.get('id')}")
        by_id[n.get("id")] = n
    for g in graph.get("groups", []):
        need(re.fullmatch(r"#[0-9a-f]{6}", g.get("color", "")), f"group {g.get('id')}: color must be #rrggbb")
    for k in graph.get("edge_kinds", []):
        need(re.fullmatch(r"#[0-9a-f]{6}", k.get("color", "")), f"edge kind {k.get('id')}: color must be #rrggbb")
    need(len(views) == len(graph.get("views", [])), "view id repeated")
    for n in nodes:
        nid = n.get("id")
        need(n.get("label"), f"node {nid}: no label")
        need(n.get("group") in groups, f"node {nid}: unknown group {n.get('group')}")
        need(n.get("part") in PARTS, f"node {nid}: part must be core or option")
        need(2 <= sentences(n.get("summary", "")) <= 4, f"node {nid}: summary needs 2 to 4 sentences")
        need(n.get("views"), f"node {nid}: no view shows it")
        for vid in n.get("views", []):
            need(vid in views, f"node {nid}: unknown view {vid}")
        need(isinstance(n.get("sources"), list) and n["sources"], f"node {nid}: no sources")
        for fact in n.get("facts", []):
            need(isinstance(fact, list) and len(fact) == 2 and all(isinstance(x, str) and x for x in fact),
                 f"node {nid}: a fact must be [name, value]")
    for vid, v in views.items():
        need(v.get("name") and v.get("text") and "\n" not in v.get("text", ""), f"view {vid}: needs a name and a one-line text")
        listed = v.get("nodes", [])
        need(len(set(listed)) == len(listed), f"view {vid}: node listed twice")
        shown = [n["id"] for n in nodes if vid in n.get("views", [])]
        need(set(listed) == set(shown),
             f"view {vid}: nodes differ from the views fields: {sorted(set(listed) ^ set(shown))}")
        framed = []
        for f in v.get("frames", []):
            need(f.get("id") and f.get("label"), f"view {vid}: a frame needs an id and a label")
            for nid in f.get("nodes", []):
                need(nid in listed, f"view {vid}, frame {f.get('id')}: {nid} is not in the view")
                framed.append(nid)
        need(len(set(framed)) == len(framed), f"view {vid}: a node is in two frames")
    pairs = set()
    for e in graph.get("edges", []):
        key = (e.get("from"), e.get("to"))
        need(e.get("from") in by_id and e.get("to") in by_id, f"edge {key}: unknown node")
        need(e.get("from") != e.get("to"), f"edge {key}: a loop")
        need(key not in pairs, f"edge {key}: repeated")
        pairs.add(key)
        need(e.get("kind") in kinds, f"edge {key}: unknown kind {e.get('kind')}")
        need(e.get("label"), f"edge {key}: no label")
        need(e.get("part", "core") in PARTS, f"edge {key}: part must be core or option")
        if e.get("from") in by_id and e.get("to") in by_id:
            need(edge_views(e, by_id, views), f"edge {key}: no view shows both nodes")
            for vid in e.get("views", []):
                need(vid in views and vid in by_id[e["from"]]["views"] and vid in by_id[e["to"]]["views"],
                     f"edge {key}: view {vid} does not show both nodes")
    tour_views = set()
    for t in graph.get("tours", []):
        tid = t.get("id")
        need(t.get("view") in views, f"tour {tid}: unknown view")
        need(t.get("name") and t.get("steps"), f"tour {tid}: needs a name and steps")
        tour_views.add(t.get("view"))
        for i, step in enumerate(t.get("steps", []), 1):
            need(step.get("nodes") and 1 <= sentences(step.get("text", "")) <= 3, f"tour {tid}, step {i}: needs nodes and a short text")
            for nid in step.get("nodes", []):
                need(nid in views.get(t.get("view"), {}).get("nodes", []), f"tour {tid}, step {i}: {nid} is not in the view")
    for vid in views:
        need(vid in tour_views, f"view {vid}: no tour")
    if root is not None:
        cited = [s for n in nodes for s in n.get("sources", [])]
        cited += [s for v in views.values() for s in v.get("sources", [])]
        for src in cited:
            path = src.split("#", 1)[0]
            need(path and not path.startswith(("/", "..")) and (root / path).exists(), f"source not found: {src}")
    if problems:
        raise GraphError("\n".join(problems))


def edge_views(edge, by_id, views):
    both = [vid for vid in views if vid in by_id[edge["from"]]["views"] and vid in by_id[edge["to"]]["views"]]
    if "views" in edge:
        both = [vid for vid in both if vid in edge["views"]]
    return both


# ---------------------------------------------------------------- overlay

def merge_overlay(graph, overlay):
    """Return a copy of graph with the private values of one host."""
    g = copy.deepcopy(graph)
    allowed = {"edition", "title", "nodes", "add_nodes", "edges", "remove_edges", "add_edges", "views", "tours", "comment"}
    unknown = set(overlay) - allowed
    if unknown:
        raise GraphError(f"overlay: unknown keys {sorted(unknown)}")
    if "title" in overlay:
        g["title"] = overlay["title"]
    if "edition" in overlay:
        g["edition"] = {"name": overlay["edition"].get("name", ""), "banner": overlay["edition"].get("banner", "")}
    by_id = {n["id"]: n for n in g["nodes"]}
    views = {v["id"]: v for v in g["views"]}
    for nid, change in overlay.get("nodes", {}).items():
        if nid not in by_id:
            raise GraphError(f"overlay: unknown node {nid}")
        node = by_id[nid]
        for key in ("label", "summary", "part", "group", "sources"):
            if key in change:
                node[key] = change[key]
        facts = dict(node.get("facts", []))
        for name, value in change.get("facts", []):
            if value is None:
                facts.pop(name, None)
            else:
                facts[name] = value
        node["facts"] = [[k, v] for k, v in facts.items()]
        for vid in change.get("add_views", []):
            if vid not in node["views"]:
                node["views"].append(vid)
                views[vid]["nodes"].append(nid)
        for vid in change.get("remove_views", []):
            if vid in node["views"]:
                node["views"].remove(vid)
                views[vid]["nodes"].remove(nid)
                for f in views[vid].get("frames", []):
                    if nid in f["nodes"]:
                        f["nodes"].remove(nid)
    for node in overlay.get("add_nodes", []):
        if node.get("id") in by_id:
            raise GraphError(f"overlay: node {node.get('id')} exists")
        node = copy.deepcopy(node)
        node.setdefault("facts", [])
        g["nodes"].append(node)
        by_id[node["id"]] = node
        for vid in node.get("views", []):
            if vid not in views:
                raise GraphError(f"overlay: node {node['id']}: unknown view {vid}")
            views[vid]["nodes"].append(node["id"])
    edges = {(e["from"], e["to"]): e for e in g["edges"]}
    for pair in overlay.get("remove_edges", []):
        if tuple(pair) not in edges:
            raise GraphError(f"overlay: no edge {pair}")
        g["edges"].remove(edges.pop(tuple(pair)))
    for key, change in overlay.get("edges", {}).items():
        pair = tuple(part.strip() for part in key.split("->"))
        if pair not in edges:
            raise GraphError(f"overlay: no edge {key}")
        edges[pair].update(change)
    for edge in overlay.get("add_edges", []):
        g["edges"].append(copy.deepcopy(edge))
    for vid, change in overlay.get("views", {}).items():
        if vid not in views:
            raise GraphError(f"overlay: unknown view {vid}")
        view = views[vid]
        for key in ("name", "text", "sources"):
            if key in change:
                view[key] = change[key]
        frames = {f["id"]: f for f in view.get("frames", [])}
        for fid, fchange in change.get("frames", {}).items():
            if fid not in frames:
                frames[fid] = {"id": fid, "label": fchange.get("label", fid), "nodes": []}
                view.setdefault("frames", []).append(frames[fid])
            if "label" in fchange:
                frames[fid]["label"] = fchange["label"]
            frames[fid]["nodes"].extend(fchange.get("add_nodes", []))
    tours = {t["id"]: t for t in g["tours"]}
    for tid, change in overlay.get("tours", {}).items():
        if tid not in tours:
            raise GraphError(f"overlay: unknown tour {tid}")
        if "name" in change:
            tours[tid]["name"] = change["name"]
        if "steps" in change:
            tours[tid]["steps"] = copy.deepcopy(change["steps"])
        tours[tid]["steps"].extend(copy.deepcopy(change.get("append_steps", [])))
    return g


# ---------------------------------------------------------------- layout

def label_lines(label):
    """One line, or two lines split after a separator so that the longer line is short."""
    if len(label) <= LINE_MAX:
        return [label]
    best = None
    for i, ch in enumerate(label[:-1]):
        if ch in "/ ,:._-":
            first, second = label[:i + 1].rstrip(), label[i + 1:].lstrip()
            cost = max(len(first), len(second)) + (0 if ch in "/ ," else 3)
            if first and second and (best is None or cost < best[0]):
                best = (cost, [first, second])
    return best[1] if best else [label]


def node_width(label):
    return int(math.ceil(max(MIN_W - 34, max(len(line) for line in label_lines(label)) * LABEL_CHAR) + 34))


def layout_view(view, by_id, edges):
    """Layered layout. A frame is a band of slots over its layers. Return the better of LR and TB."""
    ids = list(view["nodes"])
    order = {nid: i for i, nid in enumerate(ids)}
    succ, pred = defaultdict(list), defaultdict(list)
    for e in edges:
        succ[e["from"]].append(e["to"])
        pred[e["to"]].append(e["from"])
    # Break cycles with a depth-first search in the authored order.
    state, back = {}, set()

    def visit(start):
        stack = [(start, iter(succ[start]))]
        state[start] = 1
        while stack:
            nid, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[nid] = 2
                stack.pop()
            elif state.get(nxt) == 1:
                back.add((nid, nxt))
            elif nxt not in state:
                state[nxt] = 1
                stack.append((nxt, iter(succ[nxt])))

    for nid in ids:
        if nid not in state:
            visit(nid)
    fwd_succ = {nid: [m for m in succ[nid] if (nid, m) not in back] for nid in ids}
    fwd_pred = {nid: [m for m in pred[nid] if (m, nid) not in back] for nid in ids}
    # Longest path from the sources; then pull each source next to its first successor.
    layer = {}
    remaining = dict((nid, len(fwd_pred[nid])) for nid in ids)
    ready = [nid for nid in ids if remaining[nid] == 0]
    topo = []
    while ready:
        nid = ready.pop(0)
        topo.append(nid)
        layer[nid] = max([layer[p] + 1 for p in fwd_pred[nid]], default=0)
        for m in fwd_succ[nid]:
            remaining[m] -= 1
            if remaining[m] == 0:
                ready.append(m)
                ready.sort(key=order.get)
    for nid in reversed(topo):
        if not fwd_pred[nid] and fwd_succ[nid]:
            layer[nid] = min(layer[m] for m in fwd_succ[nid]) - 1
    low = min(layer.values())
    layer = {nid: layer[nid] - low for nid in ids}
    frames = view.get("frames", [])
    frame_of = {nid: f["id"] for f in frames for nid in f["nodes"]}
    neighbours = {nid: succ[nid] + pred[nid] for nid in ids}
    slot, occupied, blocked, bands = {}, defaultdict(set), defaultdict(set), []
    # Frame bands: slots that no other band uses in the same layers, one free slot between bands.
    for f in frames:
        layers = [layer[nid] for nid in f["nodes"]]
        lo, hi, size = min(layers), max(layers), max(Counter(layers).values())
        top = 0
        while any(b[0] <= hi and lo <= b[1] and top <= b[2] + b[3] and b[2] <= top + size for b in bands):
            top += 1
        bands.append((lo, hi, top, size, f))
        for col in range(lo, hi + 1):
            blocked[col].update(range(max(0, top - 1), top + size + 1))
        for col in range(lo, hi + 1):
            members = [nid for nid in f["nodes"] if layer[nid] == col]

            def bary(nid):
                known = [slot[m] for m in pred[nid] if m in slot]
                return (sum(known) / len(known)) if known else top
            members.sort(key=lambda nid: (bary(nid), order[nid]))
            first = top + (size - len(members)) // 2
            for i, nid in enumerate(members):
                slot[nid] = first + i
                occupied[col].add(first + i)
    # Free nodes: near the mean slot of their placed neighbours, outside the bands.
    free = [nid for nid in ids if nid not in frame_of]
    while free:
        ready = [nid for nid in free if any(m in slot for m in neighbours[nid])]
        if not ready:
            ready = [min(free, key=lambda nid: (layer[nid], order[nid]))]

        def desired(nid):
            known = [slot[m] for m in neighbours[nid] if m in slot]
            return int(math.floor(sum(known) / len(known) + 0.5)) if known else 0
        wish = {nid: desired(nid) for nid in ready}
        for nid in sorted(ready, key=lambda n: (layer[n], wish[n], order[n])):
            col, want = layer[nid], wish[nid]
            for step in range(0, 400):
                cands = [want + step, want - step] if step else [want]
                hit = [r for r in cands if r >= 0 and r not in occupied[col] and r not in blocked[col]]
                if hit:
                    slot[nid] = hit[0]
                    occupied[col].add(hit[0])
                    break
            free.remove(nid)
    low = min(slot.values())
    slot = {nid: s - low for nid, s in slot.items()}
    bands = [(lo, hi, top - low, size, f) for lo, hi, top, size, f in bands]
    widths = {nid: node_width(by_id[nid]["label"]) for nid in ids}
    options = [place(ids, layer, slot, bands, widths, "LR"), place(ids, layer, slot, bands, widths, "TB")]
    score = [min(CANVAS[0] / o["width"], CANVAS[1] / o["height"]) for o in options]
    return options[1] if score[1] > score[0] * 1.05 else options[0]


def place(ids, layer, slot, bands, widths, direction):
    """Pixel boxes [x, y, w, h] and frame rectangles for one direction."""
    y0 = MARGIN + FRAME_PAD_TOP
    boxes, rects = {}, []
    if direction == "LR":
        cols = max(layer.values()) + 1
        col_w = [max([widths[nid] for nid in ids if layer[nid] == c], default=MIN_W) for c in range(cols)]
        col_x, x = [], MARGIN
        for c in range(cols):
            col_x.append(x)
            x += col_w[c] + COL_GAP
        for nid in ids:
            c = layer[nid]
            boxes[nid] = [col_x[c] + (col_w[c] - widths[nid]) // 2, y0 + slot[nid] * ROW_H, widths[nid], NODE_H]
        for lo, hi, top, size, f in bands:
            rects.append({"id": f["id"], "label": f["label"], "x": col_x[lo] - FRAME_PAD, "y": y0 + top * ROW_H - FRAME_PAD_TOP,
                          "w": col_x[hi] + col_w[hi] - col_x[lo] + 2 * FRAME_PAD,
                          "h": (size - 1) * ROW_H + NODE_H + FRAME_PAD_TOP + FRAME_PAD})
    else:
        slots = max(slot.values()) + 1
        slot_w = [max([widths[nid] for nid in ids if slot[nid] == s], default=EMPTY_SLOT) for s in range(slots)]
        slot_x, x = [], MARGIN
        for s in range(slots):
            slot_x.append(x)
            x += slot_w[s] + SLOT_GAP
        for nid in ids:
            s = slot[nid]
            boxes[nid] = [slot_x[s] + (slot_w[s] - widths[nid]) // 2, y0 + layer[nid] * LAYER_H, widths[nid], NODE_H]
        for lo, hi, top, size, f in bands:
            last = top + size - 1
            rects.append({"id": f["id"], "label": f["label"], "x": slot_x[top] - FRAME_PAD, "y": y0 + lo * LAYER_H - FRAME_PAD_TOP,
                          "w": slot_x[last] + slot_w[last] - slot_x[top] + 2 * FRAME_PAD,
                          "h": (hi - lo) * LAYER_H + NODE_H + FRAME_PAD_TOP + FRAME_PAD})
    width = max([b[0] + b[2] for b in boxes.values()] + [r["x"] + r["w"] for r in rects]) + MARGIN
    height = max([b[1] + b[3] for b in boxes.values()] + [r["y"] + r["h"] for r in rects]) + MARGIN
    return {"dir": direction, "nodes": boxes, "frames": rects, "width": int(width), "height": int(height)}


def check_layout(views, layout):
    """Raise GraphError when two nodes overlap or a free node lies in a frame."""
    problems = []
    for v in views:
        lay = layout[v["id"]]
        boxes = lay["nodes"]
        ids = list(boxes)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                p, q = boxes[a], boxes[b]
                if p[0] < q[0] + q[2] and q[0] < p[0] + p[2] and p[1] < q[1] + q[3] and q[1] < p[1] + p[3]:
                    problems.append(f"view {v['id']}: {a} overlaps {b}")
        framed = {nid: f["id"] for f in v.get("frames", []) for nid in f["nodes"]}
        for rect in lay["frames"]:
            for nid, p in boxes.items():
                inside = (p[0] < rect["x"] + rect["w"] and rect["x"] < p[0] + p[2]
                          and p[1] < rect["y"] + rect["h"] and rect["y"] < p[1] + p[3])
                if framed.get(nid) == rect["id"] and not inside:
                    problems.append(f"view {v['id']}: {nid} is outside its frame")
                if framed.get(nid) != rect["id"] and inside:
                    problems.append(f"view {v['id']}: {nid} lies in frame {rect['id']}")
    if problems:
        raise GraphError("\n".join(problems))


# ---------------------------------------------------------------- page

def source_entry(src, link_base):
    """A source with a relative link, or plain text for a path that must not open in a browser."""
    path = src.split("#", 1)[0]
    safe = (re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*(#[A-Za-z0-9_-]+)?", src)
            and ".." not in path.split("/")
            and path != ".env" and not path.startswith(("secrets/", "state/", ".env/")))
    return {"text": src, "href": f"{link_base}/{src}" if safe else None}


def page_data(graph, link_base):
    by_id = {n["id"]: n for n in graph["nodes"]}
    views = {v["id"]: v for v in graph["views"]}
    layout = {}
    edges_out = []
    for e in graph["edges"]:
        shown = edge_views(e, by_id, views)
        part = e.get("part") or ("core" if by_id[e["from"]]["part"] == by_id[e["to"]]["part"] == "core" else "option")
        edges_out.append({"from": e["from"], "to": e["to"], "kind": e["kind"], "label": e["label"], "part": part, "views": shown})
    for v in graph["views"]:
        in_view = [e for e in edges_out if v["id"] in e["views"]]
        layout[v["id"]] = layout_view(v, by_id, in_view)
    check_layout(graph["views"], layout)
    nodes_out = []
    for n in graph["nodes"]:
        nodes_out.append({"id": n["id"], "label": n["label"], "lines": label_lines(n["label"]), "group": n["group"], "part": n["part"],
                          "summary": n["summary"], "facts": n.get("facts", []), "views": n["views"],
                          "sources": [source_entry(s, link_base) for s in n["sources"]]})
    views_out = [{"id": v["id"], "name": v["name"], "text": v["text"], "nodes": v["nodes"],
                  "frames": v.get("frames", []), "sources": [source_entry(s, link_base) for s in v.get("sources", [])]}
                 for v in graph["views"]]
    return {"title": graph["title"], "edition": graph["edition"], "groups": graph["groups"],
            "edge_kinds": graph["edge_kinds"], "nodes": nodes_out, "edges": edges_out,
            "views": views_out, "tours": graph["tours"], "layout": layout}


def render(data):
    payload = json.dumps(data, indent=1, ensure_ascii=True)
    # The JSON sits in a script element: no "</" may end it early.
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    edition = data["edition"]
    title = f"{data['title']} ({edition['name']})" if edition.get("name") else data["title"]
    html = TEMPLATE.replace("@@TITLE@@", escape_html(title)).replace("@@DATA@@", payload)
    return html.encode("utf-8")


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------- build and guards

def link_root(out):
    """The checkout of an output path: the parent of its first .local directory, else ROOT."""
    # A checkout can itself live below .local/worktrees/.
    if out.is_relative_to(ROOT):
        return ROOT
    parts = out.parts
    if ".local" in parts:
        return Path(*parts[:parts.index(".local")])
    return ROOT


def check_out_path(out, overlay):
    out = Path(os.path.abspath(out))
    if overlay is not None:
        if ".local" not in out.parts:
            raise GraphError(f"refused: with --overlay the output must be under .local/, not {out.name}")
        if out == Path(os.path.abspath(OUT)) or (ROOT / "docs") in out.parents:
            raise GraphError("refused: with --overlay the builder never writes under docs/")
    return out


def env_secrets(root):
    """Values of secret keys in root/.env, except the defaults of .env.example. Never printed."""
    env, example = root / ".env", root / ".env.example"
    if not env.is_file():
        return []
    defaults = set()
    if example.is_file():
        for line in example.read_text(errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                defaults.add(line.split("=", 1)[1].strip().strip("'\""))
    found = []
    for line in env.read_text(errors="replace").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if SECRET_ENV_NAME.search(key) and len(value) >= 8 and value not in defaults:
            found.append((key, value))
    return found


def guard_secrets(html, root):
    text = html.decode("utf-8")
    if SECRET_TEXT.search(text):
        raise GraphError("refused: the output contains text with the form of a key or token")
    for key, value in env_secrets(root):
        if value in text:
            raise GraphError(f"refused: the output contains the value of {key} from .env")


def build(source=SOURCE, overlay=None, out=OUT):
    """Return the page bytes. Validate the source, then the merged graph and the layout."""
    out = check_out_path(out, overlay)
    graph = load_json(source)
    validate(graph, root=ROOT)
    if overlay is not None:
        graph = merge_overlay(graph, load_json(overlay))
        validate(graph)
    base = os.path.relpath(link_root(out), out.parent).replace(os.sep, "/")
    html = render(page_data(graph, base))
    guard_secrets(html, link_root(out))
    return html


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise GraphError(f"cannot read {path}: {err}") from None


# ---------------------------------------------------------------- self-test

def luminance(color):
    def channel(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(color[i:i + 2]) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def fixture():
    src = "scripts/build-architecture-html.py"
    node = lambda nid, group, part: {"id": nid, "label": nid, "group": group, "part": part, "views": ["v1"],
                                     "summary": f"Fixture node {nid}. It has two sentences.", "sources": [src]}
    return {
        "schema": 1, "title": "Fixture", "edition": {"name": "public edition", "banner": ""},
        "groups": [{"id": "a", "label": "Group A", "color": "#7aa2f7"}, {"id": "b", "label": "Group B", "color": "#9ece6a"}],
        "edge_kinds": [{"id": "request", "label": "Request", "color": "#7aa2f7"}],
        "views": [{"id": "v1", "name": "View one", "text": "One view.", "sources": [src],
                   "nodes": ["n1", "n2", "n3", "n4", "n5"],
                   "frames": [{"id": "f1", "label": "Frame", "nodes": ["n2", "n3"]}]}],
        "nodes": [node("n1", "a", "core"), node("n2", "b", "core"), node("n3", "b", "option"),
                  node("n4", "a", "core"), node("n5", "a", "option")],
        "edges": [{"from": "n1", "to": "n2", "kind": "request", "label": "first"},
                  {"from": "n2", "to": "n3", "kind": "request", "label": "second"},
                  {"from": "n2", "to": "n4", "kind": "request", "label": "third"},
                  {"from": "n4", "to": "n1", "kind": "request", "label": "back"},
                  {"from": "n5", "to": "n4", "kind": "request", "label": "side"}],
        "tours": [{"id": "t1", "view": "v1", "name": "Tour", "steps": [{"nodes": ["n1", "n2"], "text": "Step one."}]}],
    }


def selftest():
    failures = []

    def expect(cond, message):
        if not cond:
            failures.append(message)

    with tempfile.TemporaryDirectory(prefix="arch-selftest-") as tmp:
        tmp = Path(tmp)
        src = tmp / "graph.json"
        src.write_text(json.dumps(fixture()))
        overlay = {"edition": {"name": "private edition, this host", "banner": "private edition, this host"},
                   "nodes": {"n1": {"label": "n1-host", "facts": [["Port", "1234"]]}},
                   "add_nodes": [{"id": "h1", "label": "h1", "group": "a", "part": "option", "views": ["v1"],
                                  "summary": "A host node. It exists only here.", "sources": [".local/notes.md"]}],
                   "add_edges": [{"from": "n4", "to": "h1", "kind": "request", "label": "host"}]}
        ov = tmp / "overlay.json"
        ov.write_text(json.dumps(overlay))
        first = build(src, out=tmp / "public.html")
        expect(first == build(src, out=tmp / "public.html"), "two builds differ")
        text = first.decode()
        data = json.loads(text.split('id="graph-data">', 1)[1].split("</script>", 1)[0].replace("<\\/", "</"))
        expect(len(data["layout"]["v1"]["nodes"]) == 5, "layout misses a node")
        expect("</script" not in text.split('id="graph-data">', 1)[1].split("</script>", 1)[0], "data ends the script early")
        for needle in ("<link", " src=", "@import", "fetch(", "XMLHttpRequest", "http-equiv=\"refresh"):
            expect(needle not in text, f"page has {needle}")
        expect(not re.search(r"url\((?!#)", text), "page has a url() that is not a local reference")
        expect("default-src 'none'" in text, "page lacks the content security policy")
        private = build(src, ov, tmp / ".local" / "private.html")
        ptext = private.decode()
        expect("n1-host" in ptext and "private edition, this host" in ptext and '"h1"' in ptext, "overlay not applied")
        for bad in (OUT, ROOT / "docs" / "other.html", tmp / "private.html"):
            try:
                build(src, ov, bad)
                failures.append(f"overlay output accepted at {bad.name}")
            except GraphError:
                pass
        broken = fixture()
        broken["nodes"][0]["group"] = "missing"
        src.write_text(json.dumps(broken))
        try:
            build(src, out=tmp / "public.html")
            failures.append("an unknown group passed the validation")
        except GraphError:
            pass
        try:
            guard_secrets(("sk-" + "a" * 20).encode(), tmp)
            failures.append("the secret guard missed a key form")
        except GraphError:
            pass
    pairs = [(TEXT, PANEL_BG), (MUTED, PANEL_BG), (TEXT, NODE_BG), (MUTED, NODE_BG), (TEXT, PAGE_BG), (MUTED, PAGE_BG)]
    # Literal colors of TEMPLATE: links, the banner, the second panel and the hover, selection and tour fills.
    pairs += [("#8cb4ff", PANEL_BG), ("#1a1300", "#f5c542"), (TEXT, "#1b2330"), (MUTED, "#1b2330"),
              (TEXT, "#1d2635"), (TEXT, "#1f2a3b"), (TEXT, "#22304a"), (MUTED, "#22304a"), (TEXT, "#0b0f15"), (MUTED, "#0b0f15")]
    graph = load_json(SOURCE) if SOURCE.exists() else fixture()
    pairs += [(g["color"], bg) for g in graph["groups"] for bg in (PANEL_BG, NODE_BG, "#1b2330")]
    for fg, bg in pairs:
        expect(contrast(fg, bg) >= 4.5, f"contrast {fg} on {bg} is below 4.5")
    for message in failures:
        print(f"selftest: FAIL: {message}", file=sys.stderr)
    if not failures:
        print("selftest: ok")
    return 0 if not failures else 1


# ---------------------------------------------------------------- command

def shown(path):
    """A path for messages: relative to the repo when it is inside it."""
    try:
        return str(Path(os.path.abspath(path)).relative_to(ROOT))
    except ValueError:
        return str(path)


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the architecture page from the graph source.")
    p.add_argument("--source", type=Path, default=SOURCE, help="graph source (default docs/diagrams/architecture-graph.json)")
    p.add_argument("--overlay", type=Path, help="private overlay of one host; needs --out under .local/")
    p.add_argument("--out", type=Path, default=OUT, help="output file (default docs/architecture.html)")
    p.add_argument("--check", action="store_true", help="compare a build with the output file; exit 1 on a difference")
    p.add_argument("--selftest", action="store_true", help="build a small fixture and check the builder")
    a = p.parse_args(argv)
    if a.selftest:
        return selftest()
    try:
        html = build(a.source, a.overlay, a.out)
    except GraphError as err:
        print(f"build-architecture-html: {err}", file=sys.stderr)
        return 2
    if a.check:
        if not a.out.is_file() or a.out.read_bytes() != html:
            print(f"build-architecture-html: {shown(a.out)} differs from a fresh build; run the builder", file=sys.stderr)
            return 1
        print(f"build-architecture-html: {shown(a.out)} is current")
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_name(a.out.name + ".tmp")
    tmp.write_bytes(html)
    os.replace(tmp, a.out)
    print(f"build-architecture-html: wrote {shown(a.out)} ({len(html)} bytes)")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
<title>@@TITLE@@</title>
<style>
:root {
  --bg: #0d1117; --panel: #141a23; --panel2: #1b2330; --line: #2b3545; --text: #e6edf3; --muted: #a3adbb;
  --node: #161d29; --focus: #f5c542; --mono: ui-monospace, "SFMono-Regular", Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text); font: 14px/1.45 var(--sans); }
button, input, select { font: inherit; color: var(--text); background: var(--panel2); border: 1px solid var(--line); border-radius: 6px; }
button { cursor: pointer; padding: 4px 10px; }
button:hover { border-color: #4b5a70; }
button:focus-visible, input:focus-visible, select:focus-visible, .node:focus-visible, a:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
a { color: #8cb4ff; }
#app { display: grid; grid-template-rows: auto 1fr; height: 100vh; }
header { display: flex; align-items: center; gap: 16px; padding: 10px 16px; border-bottom: 1px solid var(--line); background: var(--panel); flex-wrap: wrap; }
header h1 { font-size: 17px; margin: 0; font-weight: 650; }
#edition { font-size: 12px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; }
#banner { background: #f5c542; color: #1a1300; font-weight: 700; padding: 4px 12px; border-radius: 6px; text-transform: uppercase; letter-spacing: .04em; font-size: 12px; }
#help { color: var(--muted); font-size: 12px; margin-left: auto; }
#main { display: grid; grid-template-columns: 290px 1fr 360px; min-height: 0; }
aside { background: var(--panel); overflow-y: auto; min-height: 0; }
#sidebar { border-right: 1px solid var(--line); padding: 8px 14px 24px; }
#detail { border-left: 1px solid var(--line); padding: 14px 16px 24px; }
aside h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 18px 0 8px; font-weight: 650; }
#views button { display: block; width: 100%; text-align: left; margin: 0 0 6px; padding: 7px 10px; background: transparent; }
#views button[aria-current="true"] { background: #22304a; border-color: #7aa2f7; }
#views .count { color: var(--muted); font-size: 12px; float: right; }
#search { width: 100%; padding: 6px 9px; }
#results { list-style: none; margin: 8px 0 0; padding: 0; max-height: 230px; overflow-y: auto; }
#results button { width: 100%; text-align: left; margin-bottom: 4px; background: transparent; }
#results .where { display: block; color: var(--muted); font-size: 12px; }
.filters label { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 13px; cursor: pointer; }
.swatch { width: 12px; height: 12px; border-radius: 3px; display: inline-block; flex: none; }
.legend-row { display: flex; align-items: center; gap: 8px; font-size: 13px; margin: 4px 0; }
.legend-row svg { flex: none; }
#path-form select { width: 100%; margin-bottom: 6px; padding: 5px; }
#path-form .row { display: flex; gap: 6px; }
#path-status { color: var(--muted); font-size: 13px; margin: 6px 0; }
#path-steps { margin: 0; padding-left: 20px; font-size: 13px; }
#path-steps button { background: none; border: none; padding: 0; color: #8cb4ff; text-decoration: underline; }
#canvas-wrap { position: relative; min-width: 0; min-height: 0; display: grid; grid-template-rows: auto 1fr auto; }
#view-head { padding: 10px 16px 6px; border-bottom: 1px solid var(--line); }
#view-head h2 { margin: 0; font-size: 16px; }
#view-head p { margin: 2px 0 0; color: var(--muted); }
#canvas { width: 100%; height: 100%; display: block; cursor: grab; touch-action: none; user-select: none; }
#canvas.panning { cursor: grabbing; }
#zoom { position: absolute; right: 14px; top: 70px; display: flex; flex-direction: column; gap: 6px; }
#zoom button { width: 36px; padding: 4px 0; }
#tooltip { position: fixed; pointer-events: none; background: #0b0f15; border: 1px solid #4b5a70; padding: 5px 9px; border-radius: 6px; font-size: 12px; max-width: 360px; z-index: 5; }
#tooltip .t { font-family: var(--mono); font-weight: 650; }
#tooltip .s { color: var(--muted); }
#tourbar { border-top: 1px solid var(--line); background: var(--panel); padding: 8px 14px; display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; align-items: center; }
#tourbar .controls { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
#tour-step { color: var(--muted); font-size: 13px; min-width: 90px; }
#tour-text { margin: 0; min-height: 1.45em; }
.frame rect { fill: rgba(122, 162, 247, .045); stroke: #3a4a64; stroke-dasharray: 2 4; }
.frame text { fill: var(--muted); font-size: 12px; font-weight: 650; letter-spacing: .03em; }
.edge { fill: none; stroke-width: 1.6; opacity: .78; transition: opacity .15s; }
.edge.option { stroke-dasharray: 6 5; }
.edge.focus { stroke-width: 2.6; opacity: 1; }
.edge.dim { opacity: .09; }
.edge-hit { fill: none; stroke: transparent; stroke-width: 12; }
.elabel { font-size: 11px; fill: var(--text); paint-order: stroke; stroke: #0d1117; stroke-width: 4px; stroke-linejoin: round; display: none; pointer-events: none; }
.elabel.show { display: block; }
.node { cursor: pointer; transition: opacity .15s; }
.node .box { fill: var(--node); stroke-width: 1.6; }
.node.option .box { stroke-dasharray: 6 4; }
.node .label { font: 600 12px var(--mono); fill: var(--text); }
.node:hover .box { fill: #1d2635; }
.node.focus .box { stroke-width: 2.4; }
.node.sel .box { stroke-width: 3.2; fill: #1f2a3b; }
.node.match .box { stroke: var(--focus) !important; stroke-width: 2.6; }
.node.tour .box { fill: #22304a; stroke-width: 3; }
.node.dim { opacity: .2; }
.hidden { display: none; }
#detail .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.chip { font-size: 12px; border: 1px solid var(--line); border-radius: 999px; padding: 1px 9px; background: var(--panel2); }
button.chip { cursor: pointer; }
#detail h3 { font: 650 16px var(--mono); margin: 4px 0; word-break: break-word; }
#detail p.summary { margin: 8px 0 12px; }
#detail table { border-collapse: collapse; width: 100%; font-size: 13px; }
#detail td { border-top: 1px solid var(--line); padding: 4px 6px 4px 0; vertical-align: top; word-break: break-word; }
#detail td:first-child { color: var(--muted); width: 38%; }
#detail ul { margin: 4px 0; padding-left: 18px; font-size: 13px; }
#detail li { margin: 3px 0; word-break: break-word; }
#detail li button { background: none; border: none; padding: 0; color: #8cb4ff; text-decoration: underline; font-family: var(--mono); font-size: 13px; text-align: left; }
#detail .note { color: var(--muted); font-size: 12px; }
.kbd { font-family: var(--mono); font-size: 12px; border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; }
@media (max-width: 1100px) { #main { grid-template-columns: 250px 1fr; } #detail { grid-column: 1 / -1; border-left: none; border-top: 1px solid var(--line); max-height: 40vh; } }
@media (prefers-reduced-motion: reduce) { .edge, .node { transition: none; } }
</style>
</head>
<body>
<div id="app">
<header>
  <h1 id="title"></h1>
  <span id="edition"></span>
  <span id="banner" role="note" hidden></span>
  <span id="help">Drag to pan. Use the wheel to zoom. Click a node for details. <span class="kbd">&larr;</span> <span class="kbd">&rarr;</span> step through a tour. <span class="kbd">Esc</span> clears the selection.</span>
</header>
<div id="main">
<aside id="sidebar" aria-label="Views, search, filters and legend">
  <h2>Views</h2>
  <div id="views"></div>
  <h2><label for="search">Search</label></h2>
  <input id="search" type="search" placeholder="Label or summary" autocomplete="off">
  <ul id="results" aria-label="Search results"></ul>
  <h2>Path finder</h2>
  <div id="path-form">
    <label class="note" for="path-from">Start node</label>
    <select id="path-from"></select>
    <label class="note" for="path-to">End node</label>
    <select id="path-to"></select>
    <div class="row"><button id="path-go" type="button">Find path</button><button id="path-clear" type="button">Clear</button></div>
    <p id="path-status" aria-live="polite"></p>
    <ol id="path-steps"></ol>
  </div>
  <h2>Filter by group</h2>
  <div id="filter-groups" class="filters"></div>
  <h2>Filter by part</h2>
  <div id="filter-parts" class="filters"></div>
  <h2>Legend</h2>
  <div id="legend"></div>
  <div class="filters"><label><input type="checkbox" id="all-labels"> Show all edge labels</label></div>
</aside>
<section id="canvas-wrap" aria-label="Graph">
  <div id="view-head"><h2 id="view-name"></h2><p id="view-text"></p></div>
  <svg id="canvas" role="img" aria-labelledby="view-name">
    <defs id="defs"></defs>
    <g id="viewport"><g id="frames"></g><g id="edges"></g><g id="nodes"></g><g id="elabels"></g></g>
  </svg>
  <div id="zoom"><button id="zoom-in" type="button" aria-label="Zoom in">+</button><button id="zoom-out" type="button" aria-label="Zoom out">&minus;</button><button id="zoom-fit" type="button" aria-label="Fit the view">Fit</button></div>
  <div id="tourbar">
    <div class="controls">
      <label class="note" for="tour-select">Tour</label>
      <select id="tour-select"></select>
      <button id="tour-start" type="button">Start</button>
      <button id="tour-prev" type="button" aria-label="Previous step">&larr; Previous</button>
      <button id="tour-next" type="button" aria-label="Next step">Next &rarr;</button>
      <button id="tour-end" type="button">End</button>
      <span id="tour-step"></span>
    </div>
    <p id="tour-text" aria-live="polite"></p>
  </div>
</section>
<aside id="detail" aria-label="Details" aria-live="polite"></aside>
</div>
</div>
<div id="tooltip" hidden></div>
<script type="application/json" id="graph-data">@@DATA@@</script>
<script>
(function () {
  'use strict';
  var G = JSON.parse(document.getElementById('graph-data').textContent);
  var NS = 'http://www.w3.org/2000/svg';
  var $ = function (id) { return document.getElementById(id); };
  var NODES = {}, GROUPS = {}, KINDS = {}, VIEWS = {}, TOURS = {};
  G.nodes.forEach(function (n) { NODES[n.id] = n; });
  G.groups.forEach(function (g) { GROUPS[g.id] = g; });
  G.edge_kinds.forEach(function (k) { KINDS[k.id] = k; });
  G.views.forEach(function (v) { VIEWS[v.id] = v; });
  G.tours.forEach(function (t) { TOURS[t.id] = t; });
  var S = { view: null, selected: null, tour: null, step: 0, path: null, query: '', hideGroups: {}, hideParts: {}, allLabels: false, cam: { x: 0, y: 0, k: 1 } };
  var svg = $('canvas'), viewport = $('viewport'), tooltip = $('tooltip');
  var nodeEls = {}, edgeEls = [], anim = null;

  function el(tag, attrs, parent, ns) {
    var e = ns === false ? document.createElement(tag) : document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, text, parent, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = text;
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function clear(e) { while (e.firstChild) e.removeChild(e.firstChild); }
  function viewEdges(vid) { return G.edges.filter(function (e) { return e.views.indexOf(vid) >= 0; }); }
  function visible(id) {
    var n = NODES[id];
    return !S.hideGroups[n.group] && !S.hideParts[n.part];
  }

  // Header, legend, filters and view buttons.
  $('title').textContent = G.title;
  $('edition').textContent = G.edition.name;
  if (G.edition.banner) { $('banner').textContent = G.edition.banner; $('banner').hidden = false; }
  document.title = G.title + (G.edition.name ? ' (' + G.edition.name + ')' : '');
  G.views.forEach(function (v) {
    var b = h('button', null, $('views'));
    b.type = 'button';
    h('span', v.name, b);
    h('span', v.nodes.length + ' nodes', b, 'count');
    b.dataset.view = v.id;
    b.addEventListener('click', function () { switchView(v.id); });
  });
  var used = {};
  G.nodes.forEach(function (n) { used[n.group] = true; });
  G.groups.filter(function (g) { return used[g.id]; }).forEach(function (g) {
    var l = h('label', null, $('filter-groups'));
    var c = h('input', null, l); c.type = 'checkbox'; c.checked = true;
    c.addEventListener('change', function () { S.hideGroups[g.id] = !c.checked; applyVisibility(); });
    var sw = h('span', null, l, 'swatch'); sw.style.background = g.color;
    h('span', g.label, l);
  });
  [['core', 'Core part (solid border)'], ['option', 'Option (dashed border)']].forEach(function (p) {
    var l = h('label', null, $('filter-parts'));
    var c = h('input', null, l); c.type = 'checkbox'; c.checked = true;
    c.addEventListener('change', function () { S.hideParts[p[0]] = !c.checked; applyVisibility(); });
    h('span', p[1], l);
  });
  (function legend() {
    var box = $('legend');
    [['core', 'Core part: solid border'], ['option', 'Option: dashed border']].forEach(function (p) {
      var row = h('div', null, box, 'legend-row');
      var s = el('svg', { width: 34, height: 18 }, row);
      el('rect', { x: 1, y: 2, width: 32, height: 14, rx: 4, fill: '#161d29', stroke: '#c0caf5', 'stroke-width': 1.6, 'stroke-dasharray': p[0] === 'option' ? '6 4' : 'none' }, s);
      h('span', p[1], row);
    });
    G.edge_kinds.forEach(function (k) {
      var row = h('div', null, box, 'legend-row');
      var s = el('svg', { width: 34, height: 12 }, row);
      el('line', { x1: 1, y1: 6, x2: 33, y2: 6, stroke: k.color, 'stroke-width': 2.2 }, s);
      h('span', k.label, row);
    });
    var row = h('div', null, box, 'legend-row');
    var s = el('svg', { width: 34, height: 12 }, row);
    el('line', { x1: 1, y1: 6, x2: 33, y2: 6, stroke: '#c0caf5', 'stroke-width': 2, 'stroke-dasharray': '6 5' }, s);
    h('span', 'An edge to an option is dashed', row);
  })();
  var defs = $('defs');
  G.edge_kinds.forEach(function (k) {
    var m = el('marker', { id: 'arrow-' + k.id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, defs);
    el('path', { d: 'M0,0 L10,5 L0,10 z', fill: k.color }, m);
  });
  G.tours.forEach(function (t) {
    var o = h('option', t.name + ' (' + VIEWS[t.view].name + ')', $('tour-select'));
    o.value = t.id;
  });

  // Geometry: a cubic curve from a side of the source to a side of the target.
  function edgeGeom(a, b, dir) {
    var ax, ay, bx, by, c1x, c1y, c2x, c2y, d;
    if (dir === 'TB') {
      ax = a[0] + a[2] / 2; bx = b[0] + b[2] / 2;
      if (b[1] >= a[1] + a[3]) { ay = a[1] + a[3]; by = b[1]; d = Math.max(30, (by - ay) / 2); c1y = ay + d; c2y = by - d; }
      else if (b[1] + b[3] <= a[1]) { ay = a[1]; by = b[1] + b[3]; d = Math.max(30, (ay - by) / 2); c1y = ay - d; c2y = by + d; }
      else { ay = a[1] + a[3]; by = b[1] + b[3]; d = 36 + Math.abs(bx - ax) * 0.18; c1y = ay + d; c2y = by + d; }
      c1x = ax; c2x = bx;
    } else {
      ay = a[1] + a[3] / 2; by = b[1] + b[3] / 2;
      if (b[0] >= a[0] + a[2]) { ax = a[0] + a[2]; bx = b[0]; d = Math.max(36, (bx - ax) / 2); c1x = ax + d; c2x = bx - d; }
      else if (b[0] + b[2] <= a[0]) { ax = a[0]; bx = b[0] + b[2]; d = Math.max(36, (ax - bx) / 2); c1x = ax - d; c2x = bx + d; }
      else { ax = a[0] + a[2]; bx = b[0] + b[2]; d = 40 + Math.abs(by - ay) * 0.22; c1x = ax + d; c2x = bx + d; }
      c1y = ay; c2y = by;
    }
    return { d: 'M' + ax + ',' + ay + ' C' + c1x + ',' + c1y + ' ' + c2x + ',' + c2y + ' ' + bx + ',' + by,
             mx: (ax + 3 * c1x + 3 * c2x + bx) / 8, my: (ay + 3 * c1y + 3 * c2y + by) / 8 };
  }

  // Render one view.
  function render() {
    var lay = G.layout[S.view], v = VIEWS[S.view];
    ['frames', 'edges', 'nodes', 'elabels'].forEach(function (id) { clear($(id)); });
    nodeEls = {}; edgeEls = [];
    lay.frames.forEach(function (f) {
      var g = el('g', { class: 'frame' }, $('frames'));
      el('rect', { x: f.x, y: f.y, width: f.w, height: f.h, rx: 12 }, g);
      var t = el('text', { x: f.x + 12, y: f.y + 19 }, g); t.textContent = f.label;
    });
    viewEdges(S.view).forEach(function (e) {
      var geo = edgeGeom(lay.nodes[e.from], lay.nodes[e.to], lay.dir), k = KINDS[e.kind];
      var p = el('path', { d: geo.d, class: 'edge ' + e.part, stroke: k.color, 'marker-end': 'url(#arrow-' + k.id + ')' }, $('edges'));
      var hit = el('path', { d: geo.d, class: 'edge-hit' }, $('edges'));
      var t = el('text', { x: geo.mx, y: geo.my - 5, 'text-anchor': 'middle', class: 'elabel' }, $('elabels'));
      t.textContent = e.label;
      var rec = { e: e, path: p, hit: hit, label: t };
      hit.addEventListener('pointerenter', function (ev) { showTip(ev, NODES[e.from].label + ' → ' + NODES[e.to].label, e.label + ' (' + k.label + ')'); t.classList.add('show'); });
      hit.addEventListener('pointermove', moveTip);
      hit.addEventListener('pointerleave', function () { hideTip(); applyHighlight(); });
      edgeEls.push(rec);
    });
    v.nodes.forEach(function (id) {
      var n = NODES[id], b = lay.nodes[id], g = GROUPS[n.group];
      var e = el('g', { class: 'node ' + n.part, transform: 'translate(' + b[0] + ',' + b[1] + ')', tabindex: 0, role: 'button', 'aria-label': n.label + ', ' + g.label + ', ' + n.part }, $('nodes'));
      e.dataset.id = id;
      el('rect', { class: 'box', width: b[2], height: b[3], rx: 9, stroke: g.color }, e);
      el('circle', { cx: 16, cy: b[3] / 2, r: 5, fill: g.color }, e);
      n.lines.forEach(function (line, i) {
        var t = el('text', { class: 'label', x: 28, y: n.lines.length === 1 ? 29 : 22 + i * 15 }, e); t.textContent = line;
      });
      e.addEventListener('pointerenter', function (ev) { showTip(ev, n.label, g.label + ' · ' + n.part); });
      e.addEventListener('pointermove', moveTip);
      e.addEventListener('pointerleave', hideTip);
      e.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); select(id); } });
      nodeEls[id] = e;
    });
    $('view-name').textContent = v.name;
    $('view-text').textContent = v.text;
    Array.prototype.forEach.call($('views').children, function (b) { b.setAttribute('aria-current', b.dataset.view === S.view ? 'true' : 'false'); });
    fillPathSelects();
    applyVisibility();
  }

  function applyVisibility() {
    Object.keys(nodeEls).forEach(function (id) { nodeEls[id].classList.toggle('hidden', !visible(id)); });
    edgeEls.forEach(function (r) {
      var hide = !visible(r.e.from) || !visible(r.e.to);
      [r.path, r.hit, r.label].forEach(function (x) { x.classList.toggle('hidden', hide); });
    });
    if (S.selected && !visible(S.selected)) S.selected = null;
    applyHighlight();
    renderDetail();
  }

  // Highlight: selection, then path, then tour step, then search.
  function focusSets() {
    var nodes = null, edges = null;
    if (S.selected) {
      nodes = {}; edges = {}; nodes[S.selected] = true;
      edgeEls.forEach(function (r, i) {
        if (r.e.from === S.selected || r.e.to === S.selected) { edges[i] = true; nodes[r.e.from] = true; nodes[r.e.to] = true; }
      });
    } else if (S.path) {
      nodes = {}; edges = {};
      S.path.nodes.forEach(function (id) { nodes[id] = true; });
      edgeEls.forEach(function (r, i) { if (S.path.edges.indexOf(r.e) >= 0) edges[i] = true; });
    } else if (S.tour) {
      nodes = {}; edges = {};
      TOURS[S.tour].steps[S.step].nodes.forEach(function (id) { nodes[id] = true; });
      edgeEls.forEach(function (r, i) { if (nodes[r.e.from] && nodes[r.e.to]) edges[i] = true; });
    } else if (S.query) {
      nodes = {}; edges = {};
      Object.keys(nodeEls).forEach(function (id) { if (matches(NODES[id], S.query)) nodes[id] = true; });
    }
    return { nodes: nodes, edges: edges };
  }
  function applyHighlight() {
    var f = focusSets(), tourNodes = {};
    if (S.tour && !S.selected && !S.path) TOURS[S.tour].steps[S.step].nodes.forEach(function (id) { tourNodes[id] = true; });
    Object.keys(nodeEls).forEach(function (id) {
      var c = nodeEls[id].classList;
      c.toggle('dim', !!f.nodes && !f.nodes[id]);
      c.toggle('focus', !!f.nodes && !!f.nodes[id]);
      c.toggle('sel', S.selected === id);
      c.toggle('tour', !!tourNodes[id]);
      c.toggle('match', !!S.query && !S.selected && matches(NODES[id], S.query));
    });
    edgeEls.forEach(function (r, i) {
      var on = !!f.edges && !!f.edges[i];
      r.path.classList.toggle('dim', !!f.nodes && !on);
      r.path.classList.toggle('focus', on);
      r.label.classList.toggle('show', S.allLabels || on);
    });
  }
  function matches(n, q) { return (n.label + ' ' + n.summary + ' ' + n.id).toLowerCase().indexOf(q) >= 0; }

  // Detail panel.
  function nodeButton(id, parent, suffix) {
    var li = h('li', null, parent), b = h('button', NODES[id].label, li);
    b.type = 'button';
    b.addEventListener('click', function () { goTo(id); });
    if (suffix) h('span', ' ' + suffix, li, 'note');
  }
  function sourceList(list, parent) {
    var ul = h('ul', null, parent);
    list.forEach(function (s) {
      var li = h('li', null, ul);
      if (s.href) { var a = h('a', s.text, li); a.href = s.href; } else h('code', s.text, li);
    });
  }
  function renderDetail() {
    var box = $('detail'); clear(box);
    if (!S.selected) {
      var v = VIEWS[S.view];
      h('h2', 'This view', box);
      h('h3', v.name, box);
      h('p', v.text, box, 'summary');
      h('p', 'Select a node to see its summary, its sources and its edges. A node that a filter hides is not shown.', box, 'note');
      h('h2', 'Sources of this view', box);
      sourceList(v.sources, box);
      h('h2', 'Count', box);
      h('p', v.nodes.length + ' nodes, ' + viewEdges(S.view).length + ' edges, ' + v.frames.length + ' frames.', box);
      return;
    }
    var n = NODES[S.selected], g = GROUPS[n.group];
    h('h2', 'Node', box);
    h('h3', n.label, box);
    var chips = h('div', null, box, 'chips');
    var c = h('span', g.label, chips, 'chip'); c.style.borderColor = g.color; c.style.color = g.color;
    h('span', n.part === 'core' ? 'core part' : 'option', chips, 'chip');
    h('p', n.summary, box, 'summary');
    if (n.facts.length) {
      var table = h('table', null, box);
      n.facts.forEach(function (f) { var tr = h('tr', null, table); h('td', f[0], tr); h('td', f[1], tr); });
    }
    h('h2', 'Views', box);
    var vc = h('div', null, box, 'chips');
    n.views.forEach(function (vid) {
      var b = h('button', VIEWS[vid].name, vc, 'chip'); b.type = 'button';
      if (vid === S.view) b.setAttribute('aria-current', 'true');
      b.addEventListener('click', function () { switchView(vid, { keep: S.selected }); });
    });
    h('h2', 'Sources', box);
    sourceList(n.sources, box);
    var outs = G.edges.filter(function (e) { return e.from === n.id; });
    var ins = G.edges.filter(function (e) { return e.to === n.id; });
    h('h2', 'Edges out (' + outs.length + ')', box);
    var ul = h('ul', null, box);
    outs.forEach(function (e) { nodeButton(e.to, ul, '— ' + e.label + (e.views.indexOf(S.view) < 0 ? ' (other view)' : '')); });
    h('h2', 'Edges in (' + ins.length + ')', box);
    ul = h('ul', null, box);
    ins.forEach(function (e) { nodeButton(e.from, ul, '— ' + e.label + (e.views.indexOf(S.view) < 0 ? ' (other view)' : '')); });
  }

  // Selection, views and camera.
  function select(id) {
    S.selected = id;
    applyHighlight(); renderDetail(); setHash();
  }
  function goTo(id) {
    if (VIEWS[S.view].nodes.indexOf(id) < 0) switchView(NODES[id].views[0], { keep: id });
    if (!visible(id)) return;
    select(id);
    var b = G.layout[S.view].nodes[id];
    fitBox(b[0] - 220, b[1] - 160, b[2] + 440, b[3] + 320, true);
  }
  function switchView(vid, opts) {
    opts = opts || {};
    if (S.tour && TOURS[S.tour].view !== vid && !opts.tour) endTour();
    S.view = vid; S.path = null;
    S.selected = opts.keep && VIEWS[vid].nodes.indexOf(opts.keep) >= 0 ? opts.keep : null;
    $('path-status').textContent = ''; clear($('path-steps'));
    render();
    if (!S.tour) { var t = G.tours.filter(function (x) { return x.view === vid; })[0]; if (t) $('tour-select').value = t.id; }
    if (!opts.noFit) fitAll(false);
    setHash();
  }
  function setCam(x, y, k) { S.cam = { x: x, y: y, k: k }; viewport.setAttribute('transform', 'translate(' + x + ',' + y + ') scale(' + k + ')'); }
  function fitBox(x, y, w, hgt, animate, maxK) {
    var W = svg.clientWidth || 800, H = svg.clientHeight || 600;
    var k = Math.min(W / w, H / hgt, maxK || 1.5); k = Math.max(0.12, k);
    var tx = W / 2 - (x + w / 2) * k, ty = H / 2 - (y + hgt / 2) * k;
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!animate || reduce) { setCam(tx, ty, k); return; }
    var from = S.cam, t0 = null;
    if (anim) cancelAnimationFrame(anim);
    function stepFn(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / 320), e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      setCam(from.x + (tx - from.x) * e, from.y + (ty - from.y) * e, from.k + (k - from.k) * e);
      anim = p < 1 ? requestAnimationFrame(stepFn) : null;
    }
    anim = requestAnimationFrame(stepFn);
  }
  function fitAll(animate) { var l = G.layout[S.view]; fitBox(0, 0, l.width, l.height, animate, 1.2); }
  function fitNodes(ids, animate) {
    var l = G.layout[S.view].nodes, x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    ids.forEach(function (id) { var b = l[id]; x1 = Math.min(x1, b[0]); y1 = Math.min(y1, b[1]); x2 = Math.max(x2, b[0] + b[2]); y2 = Math.max(y2, b[1] + b[3]); });
    fitBox(x1 - 140, y1 - 110, x2 - x1 + 280, y2 - y1 + 220, animate, 1.25);
  }
  function zoomAt(f, cx, cy) {
    var k = Math.max(0.12, Math.min(3, S.cam.k * f)); f = k / S.cam.k;
    setCam(cx - (cx - S.cam.x) * f, cy - (cy - S.cam.y) * f, k);
  }
  function setHash() {
    var hash = '#view=' + S.view + (S.selected ? '&node=' + S.selected : '');
    try { history.replaceState(null, '', hash); } catch (err) { /* file:// can refuse; the page still works */ }
  }

  // Pan, zoom and click.
  var drag = null;
  svg.addEventListener('pointerdown', function (ev) {
    if (ev.button !== 0) return;
    drag = { x: ev.clientX, y: ev.clientY, cx: S.cam.x, cy: S.cam.y, moved: false, node: ev.target.closest ? ev.target.closest('.node') : null };
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener('pointermove', function (ev) {
    if (!drag) return;
    var dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; svg.classList.add('panning'); hideTip(); }
    if (drag.moved) setCam(drag.cx + dx, drag.cy + dy, S.cam.k);
  });
  svg.addEventListener('pointerup', function () {
    if (drag && !drag.moved) {
      if (drag.node) select(drag.node.dataset.id);
      else if (S.selected) select(null);
    }
    drag = null; svg.classList.remove('panning');
  });
  svg.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var r = svg.getBoundingClientRect();
    zoomAt(Math.exp(-ev.deltaY * 0.0015), ev.clientX - r.left, ev.clientY - r.top);
  }, { passive: false });
  $('zoom-in').addEventListener('click', function () { zoomAt(1.25, svg.clientWidth / 2, svg.clientHeight / 2); });
  $('zoom-out').addEventListener('click', function () { zoomAt(0.8, svg.clientWidth / 2, svg.clientHeight / 2); });
  $('zoom-fit').addEventListener('click', function () { fitAll(true); });
  $('all-labels').addEventListener('change', function (ev) { S.allLabels = ev.target.checked; applyHighlight(); });

  // Tooltip.
  function showTip(ev, title, sub) {
    if (drag && drag.moved) return;
    clear(tooltip); h('div', title, tooltip, 't'); h('div', sub, tooltip, 's');
    tooltip.hidden = false; moveTip(ev);
  }
  function moveTip(ev) { tooltip.style.left = (ev.clientX + 14) + 'px'; tooltip.style.top = (ev.clientY + 14) + 'px'; }
  function hideTip() { tooltip.hidden = true; }

  // Search.
  $('search').addEventListener('input', function (ev) {
    S.query = ev.target.value.trim().toLowerCase();
    var box = $('results'); clear(box);
    if (S.query) {
      var hits = G.nodes.filter(function (n) { return matches(n, S.query); }).slice(0, 40);
      if (!hits.length) h('li', 'No node matches.', box, 'note');
      hits.forEach(function (n) {
        var li = h('li', null, box), b = h('button', n.label, li); b.type = 'button';
        h('span', n.views.map(function (v) { return VIEWS[v].name; }).join(', '), b, 'where');
        b.addEventListener('click', function () { goTo(n.id); });
      });
    }
    applyHighlight();
  });

  // Path finder.
  function fillPathSelects() {
    var ids = VIEWS[S.view].nodes.slice().sort(function (a, b) { return NODES[a].label.localeCompare(NODES[b].label); });
    ['path-from', 'path-to'].forEach(function (sid, i) {
      var s = $(sid); clear(s);
      ids.forEach(function (id) { var o = h('option', NODES[id].label, s); o.value = id; });
      s.selectedIndex = i === 0 ? 0 : ids.length - 1;
    });
  }
  function shortest(from, to, directed) {
    var edges = viewEdges(S.view).filter(function (e) { return visible(e.from) && visible(e.to); });
    var prev = {}, queue = [from]; prev[from] = null;
    while (queue.length) {
      var cur = queue.shift();
      if (cur === to) break;
      edges.forEach(function (e) {
        var next = e.from === cur ? e.to : (!directed && e.to === cur ? e.from : null);
        if (next && !(next in prev)) { prev[next] = { node: cur, edge: e }; queue.push(next); }
      });
    }
    if (!(to in prev)) return null;
    var nodes = [to], list = [];
    for (var at = to; prev[at]; at = prev[at].node) { nodes.unshift(prev[at].node); list.unshift(prev[at].edge); }
    return { nodes: nodes, edges: list, directed: directed };
  }
  $('path-go').addEventListener('click', function () {
    var a = $('path-from').value, b = $('path-to').value, status = $('path-status'), ol = $('path-steps');
    clear(ol); S.selected = null;
    if (a === b) { S.path = null; status.textContent = 'Select two different nodes.'; applyHighlight(); renderDetail(); return; }
    var p = shortest(a, b, true) || shortest(a, b, false);
    S.path = p;
    if (!p) status.textContent = 'No path between these nodes in this view.';
    else {
      status.textContent = 'Shortest path: ' + p.edges.length + ' steps.' + (p.directed ? '' : ' No directed path exists, so this path ignores the edge direction.');
      p.edges.forEach(function (e, i) {
        var li = h('li', null, ol), here = p.nodes[i], there = p.nodes[i + 1];
        var b1 = h('button', NODES[here].label, li); b1.type = 'button'; b1.addEventListener('click', function () { goTo(here); });
        h('span', e.from === here ? ' → ' : ' ← ', li);
        var b2 = h('button', NODES[there].label, li); b2.type = 'button'; b2.addEventListener('click', function () { goTo(there); });
        h('span', ' (' + e.label + ')', li, 'note');
      });
      fitNodes(p.nodes, true);
    }
    applyHighlight(); renderDetail();
  });
  $('path-clear').addEventListener('click', function () { S.path = null; $('path-status').textContent = ''; clear($('path-steps')); applyHighlight(); });

  // Tours.
  function showStep() {
    var t = TOURS[S.tour], st = t.steps[S.step];
    $('tour-step').textContent = 'Step ' + (S.step + 1) + ' of ' + t.steps.length;
    $('tour-text').textContent = st.text;
    $('tour-prev').disabled = S.step === 0;
    $('tour-next').disabled = S.step === t.steps.length - 1;
    S.selected = null; S.path = null;
    applyHighlight(); renderDetail();
    fitNodes(st.nodes.filter(visible).length ? st.nodes.filter(visible) : st.nodes, true);
  }
  function startTour(tid) {
    var t = TOURS[tid];
    S.tour = tid; S.step = 0;
    if (S.view !== t.view) switchView(t.view, { tour: true, noFit: true });
    $('tour-select').value = tid;
    showStep();
  }
  function endTour() {
    S.tour = null; $('tour-step').textContent = ''; $('tour-text').textContent = '';
    $('tour-prev').disabled = true; $('tour-next').disabled = true;
    applyHighlight();
  }
  function moveStep(d) {
    if (!S.tour) { startTour($('tour-select').value); return; }
    var n = S.step + d;
    if (n < 0 || n >= TOURS[S.tour].steps.length) return;
    S.step = n; showStep();
  }
  $('tour-start').addEventListener('click', function () { startTour($('tour-select').value); });
  $('tour-prev').addEventListener('click', function () { moveStep(-1); });
  $('tour-next').addEventListener('click', function () { moveStep(1); });
  $('tour-end').addEventListener('click', endTour);
  $('tour-select').addEventListener('change', function () { if (S.tour) startTour($('tour-select').value); });

  // Keyboard.
  document.addEventListener('keydown', function (ev) {
    var tag = (ev.target.tagName || '').toLowerCase();
    if (ev.key === 'Escape') {
      if (S.selected || S.path) { S.selected = null; S.path = null; $('path-status').textContent = ''; clear($('path-steps')); applyHighlight(); renderDetail(); setHash(); }
      else if (S.tour) endTour();
      else if (S.query) { $('search').value = ''; S.query = ''; clear($('results')); applyHighlight(); }
      return;
    }
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') { ev.preventDefault(); moveStep(1); }
    if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') { ev.preventDefault(); if (S.tour) moveStep(-1); }
  });
  window.addEventListener('resize', function () { if (!S.tour && !S.selected) fitAll(false); });

  // Start.
  var params = {};
  location.hash.replace(/^#/, '').split('&').forEach(function (kv) { var p = kv.split('='); if (p[0]) params[p[0]] = decodeURIComponent(p[1] || ''); });
  var start = VIEWS[params.view] ? params.view : G.views[0].id;
  endTour();
  switchView(start);
  if (params.node && NODES[params.node] && VIEWS[start].nodes.indexOf(params.node) >= 0) goTo(params.node);
  window.architecturePage = { state: S, switchView: switchView, startTour: startTour, moveStep: moveStep, select: select, goTo: goTo };
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
