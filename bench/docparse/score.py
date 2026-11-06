"""Score normalized parses against the corpus's gold labels.

Per file: text recall and precision (word multisets), reading-order accuracy,
heading F1 and level accuracy, table cell F1 and table detection F1 (headings
or tables where the file has none score 0), locator accuracy, notes membership
(decks), lost blocks and silent loss, errors, parse time and peak memory. A
parse that failed scores 0 on every metric its file's gold defines but level
accuracy, which is taken over the matched headings and so has none.
Per category and overall: the mean of each, and for every system after the
first, the paired difference from the first with a 95% bootstrap interval
(`bench.bootstrap.paired_bootstrap`, files resampled).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from bench.bootstrap import paired_bootstrap

WORD = re.compile(r"[a-z0-9]+")


def words(s: str) -> list[str]:
    s = s.lower().replace("ﬁ", "fi").replace("ﬂ", "fl").replace("’", "'")
    return WORD.findall(s)


def key(s: str) -> str:
    return " ".join(words(s))


NUMBERING = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2})*\.?|[a-z][.)])\s+", re.I)


def unnumbered(s: str) -> str:
    """`s` without a leading section or item number, which some parsers keep
    apart from the text (a list enumerator) and some drop."""
    return NUMBERING.sub("", s, count=1)


def gold_words(g: dict) -> Counter:
    c: Counter = Counter()
    for b in g["blocks"]:
        c.update(words(b["text"]))
    for t in g["tables"]:
        for r in t["rows"]:
            for cell in r:
                c.update(words(cell))
    return c


def out_words(p: dict) -> Counter:
    """The parse's words: its blocks' but a table block's, and each table's
    cells, so a table counts once however the parse writes it."""
    c: Counter = Counter()
    for w in _stream(p):
        c[w] += 1
    for t in p.get("tables", []):
        for r in t["rows"]:
            for cell in r:
                c.update(words(cell))
    return c


def prf(tp: float, n_out: float, n_gold: float) -> tuple[float, float, float]:
    pr = tp / n_out if n_out else (1.0 if not n_gold else 0.0)
    rc = tp / n_gold if n_gold else 1.0
    f = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
    return pr, rc, f


def text_scores(g: dict, p: dict) -> dict:
    gw, ow = gold_words(g), out_words(p)
    tp = sum((gw & ow).values())
    pr, rc, _ = prf(tp, sum(ow.values()), sum(gw.values()))
    return {"text_recall": rc, "text_precision": pr}


def _stream(p: dict) -> list[str]:
    """The words of the parse's blocks in order, a table block's left to its table."""
    out: list[str] = []
    for b in p.get("blocks", []):
        if b["type"] != "table":
            out.extend(words(b["text"]))
    return out


def _index(stream: list[str]) -> dict[tuple[str, ...], list[int]]:
    idx: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for i in range(len(stream) - 4):
        idx[tuple(stream[i : i + 5])].append(i)
    return idx


def order_score(g: dict, p: dict) -> float | None:
    """The share of consecutive gold text units (paragraphs, items, headings of
    five words or more) that the parse holds in the same order."""
    units = [
        words(b["text"])
        for b in g["blocks"]
        if b["kind"] in ("paragraph", "item", "heading", "code")
    ]
    units = [u for u in units if len(u) >= 5]
    if len(units) < 3:
        return None
    idx = _index(_stream(p))
    pos: list[int] = []
    last = -1
    for u in units:
        hits = idx.get(tuple(u[:5]))
        if hits:
            # a text that recurs: its next occurrence after the unit before it
            nxt = next((h for h in hits if h > last), hits[0])
            pos.append(nxt)
            last = nxt
    if len(pos) < 2:
        return 0.0
    good = sum(1 for a, b in zip(pos, pos[1:], strict=False) if a < b)
    return good / (len(units) - 1)


def heading_scores(g: dict, p: dict) -> dict:
    gold = [
        (b.get("level", 1), key(unnumbered(b["text"])))
        for b in g["blocks"]
        if b["kind"] == "heading"
    ]
    out = [
        (b.get("level") or 1, key(unnumbered(b["text"])))
        for b in p.get("blocks", [])
        if b["type"] == "heading"
    ]
    out = [o for o in out if o[1]]
    if not gold:
        # headings where the file has none are all wrong
        return {"heading_f1": 0.0, "heading_precision": 0.0} if out else {}
    used = [False] * len(out)
    pairs = []
    for gl, gt in gold:
        for i, (ol, ot) in enumerate(out):
            if not used[i] and (
                ot == gt
                or (
                    len(gt) > 12
                    and (ot.startswith(gt) or gt.startswith(ot))
                    and min(len(ot), len(gt)) / max(len(ot), len(gt)) > 0.8
                )
            ):
                used[i] = True
                pairs.append((gl, ol))
                break
    pr, rc, f = prf(len(pairs), len(out), len(gold))
    # Levels compared by rank over the matched headings: each side's distinct
    # levels in order, 1, 2, 3, ...
    grank = {lv: i for i, lv in enumerate(sorted({gl for gl, _ in pairs}))}
    orank = {lv: i for i, lv in enumerate(sorted({ol for _, ol in pairs}))}
    lvl = sum(1 for gl, ol in pairs if grank[gl] == orank[ol]) / len(pairs) if pairs else None
    return {
        "heading_f1": f,
        "heading_recall": rc,
        "heading_precision": pr,
        "heading_level_acc": lvl,
    }


def _cells(rows: list[list[str]]) -> list[list[str]]:
    return [[key(c) for c in r] for r in rows]


def table_scores(g: dict, p: dict) -> dict:
    gts = [_cells(t["rows"]) for t in g["tables"]]
    ots = [_cells(t["rows"]) for t in p.get("tables", [])]
    if not gts:
        # tables where the file has none are all wrong
        return {"table_det_f1": 0.0, "table_det_precision": 0.0} if ots else {}
    n_gold = sum(sum(1 for c in r if c) for t in gts for r in t)
    matched = 0
    out_counted = 0
    gold_hit = [0] * len(gts)
    out_hit = [False] * len(ots)
    used: set[tuple[int, int]] = set()  # gold rows a parse row already matched
    for oi, ot in enumerate(ots):
        # Each out row matches the gold row (of any gold table) sharing most cells at
        # the same column, one not matched yet on a tie; a repeat of a gold header
        # row (a table split over pages, its header printed on each) is neither a
        # hit nor a miss.
        for row in ot:
            best, best_t, best_r = 0, -1, -1
            for ti, gt in enumerate(gts):
                for ri, grow in enumerate(gt):
                    m = sum(1 for a, b in zip(row, grow, strict=False) if a and a == b)
                    if m > best or (
                        m == best and m and (best_t, best_r) in used and (ti, ri) not in used
                    ):
                        best, best_t, best_r = m, ti, ri
            if best_t >= 0 and best_r == 0 and row == gts[best_t][0] and (best_t, 0) in used:
                out_hit[oi] = True
                continue
            out_counted += sum(1 for c in row if c)
            if best_t >= 0 and (best_t, best_r) not in used:
                used.add((best_t, best_r))
                matched += best
                gold_hit[best_t] += best
                out_hit[oi] = True
    pr, rc, f = prf(matched, out_counted, n_gold)
    det_gold = sum(
        1 for ti, gt in enumerate(gts) if gold_hit[ti] >= 0.5 * sum(1 for r in gt for c in r if c)
    )
    det_rc = det_gold / len(gts)
    det_pr = sum(out_hit) / len(ots) if ots else 0.0
    det_f = 2 * det_pr * det_rc / (det_pr + det_rc) if det_pr + det_rc else 0.0
    return {
        "table_cell_f1": f,
        "table_cell_recall": rc,
        "table_cell_precision": pr,
        "table_det_f1": det_f,
    }


def locator_scores(g: dict, p: dict) -> dict:
    """The share of gold blocks and tables the parse cites on their page. A gold
    block is found in the parse block that holds its opening words, the next one
    on from the block the gold block before it was found in (text recurs), and is
    cited right when that block's pages hold its page and span no more pages
    than it does: one block over every page cites nothing. A table is cited
    right when a parse table with its header starts on its first page and
    spans no more pages than it does."""
    if g["format"] == "docx":
        return {}
    blocks = [b for b in p.get("blocks", []) if b["type"] != "table"]
    texts = [f" {key(b['text'])} " for b in blocks]  # spaced, so a probe matches whole words
    ok = n = 0
    note_ok = note_n = 0
    last = 0
    for gb in g["blocks"]:
        probe = " ".join(words(unnumbered(gb["text"]))[:6])
        if not gb.get("page") or not probe:
            continue
        n += 1
        note_n += gb["kind"] == "note"
        hits = [i for i, t in enumerate(texts) if f" {probe} " in t]
        at = next((i for i in hits if i >= last), hits[0] if hits else None)
        if at is None:
            continue
        last = max(last, at)
        b = blocks[at]
        lo, hi = b["page_start"], max(b["page_end"], b["page_start"])
        page, end = gb["page"], gb.get("page_end", gb["page"])
        if lo <= page <= hi and hi - lo <= end - page:
            ok += 1
            note_ok += gb["kind"] == "note" and b["notes"]
    for gt in g["tables"]:
        n += 1
        head = [key(c) for c in gt["rows"][0]]
        span = gt.get("page_end", 0) - gt.get("page_start", 0)
        if any(
            t["page_start"] == gt.get("page_start")
            and t["page_end"] - t["page_start"] <= span
            and [key(c) for c in t["rows"][0]] == head
            for t in p.get("tables", [])
        ):
            ok += 1
    out = {"locator_acc": ok / n} if n else {}
    if note_n:
        out["notes_acc"] = note_ok / note_n
    return out


def per_page_loss(g: dict, p: dict) -> bool:
    """Whether a gold page's text is mostly missing from the parse."""
    ow = out_words(p)
    pages: dict[int, Counter] = defaultdict(Counter)
    for b in g["blocks"]:
        if b.get("page"):
            pages[b["page"]].update(words(b["text"]))
    for c in pages.values():
        tot = sum(c.values())
        if tot >= 8 and sum((c & ow).values()) / tot < 0.5:
            return True
    return False


def lost_blocks(g: dict, p: dict) -> int:
    """Gold text blocks of two words or more whose opening words (up to five)
    the parse holds nowhere as a run, and most of whose word pairs it does not
    hold either: text the file shows that the parse dropped."""
    stream = " " + " ".join(_stream(p)) + " "
    for t in p.get("tables", []):
        stream += " ".join(" ".join(words(c)) for r in t["rows"] for c in r) + " "
    tokens = stream.split()
    pairs = set(zip(tokens, tokens[1:], strict=False))
    n = 0
    for b in g["blocks"]:
        w = words(unnumbered(b["text"]))
        if len(w) < 2 or f" {' '.join(w[:5])} " in stream:
            continue
        # held out of order (a code block's lines shuffled) is not lost: most of its word pairs are there
        own = set(zip(w, w[1:], strict=False))
        if len(own & pairs) < 0.8 * len(own):
            n += 1
    return n


def score_file(g: dict, p: dict) -> dict:
    """One file's scores. A parse that failed is scored as an empty one that
    says it is incomplete: 0 on every metric the gold defines, no level accuracy
    (no heading matched), and no time or memory, which the means leave out."""
    if "error" in p:
        s = score_file(g, {"complete": False, "blocks": [], "tables": []})
        # no time or memory: a failed parse is not faster or leaner for failing
        s.update(error=1, seconds=None, peak_mb=None)
        return s
    s: dict = {**text_scores(g, p), "error": 0}
    o = order_score(g, p)
    if o is not None:
        s["order_acc"] = o
    s.update(heading_scores(g, p))
    s.update(table_scores(g, p))
    s.update(locator_scores(g, p))
    # Silent loss: reported complete while content is missing (recall under 0.9, a
    # page mostly lost, or a block dropped, such as text only an image holds).
    s["lost_blocks"] = lost_blocks(g, p)
    lost = s["text_recall"] < 0.9 or per_page_loss(g, p) or s["lost_blocks"] > 0
    s["silent_loss"] = int(bool(p.get("complete")) and lost)
    s["complete"] = int(bool(p.get("complete")))
    s["seconds"] = p.get("seconds", 0.0)
    s["peak_mb"] = p.get("peak_mb", 0.0)
    return s


METRICS = [
    "text_recall",
    "text_precision",
    "order_acc",
    "heading_f1",
    "heading_level_acc",
    "table_cell_f1",
    "table_det_f1",
    "locator_acc",
    "notes_acc",
    "lost_blocks",
    "silent_loss",
    "error",
    "seconds",
    "peak_mb",
]
# Counted over files, not averaged.
SUMMED = {"silent_loss", "error"}


def score(
    corpus: Path, systems: list[tuple[str, Path]], split: str = "all", out_json: Path | None = None
) -> dict:
    """Print each group's table (each category, all files, all PDFs) and
    return the report; the first system is the one the others are paired with."""
    systems_read = [
        (name, {r["file"]: r for r in map(json.loads, path.read_text().splitlines())})
        for name, path in systems
    ]
    gold = [json.loads(line) for line in (corpus / "gold.jsonl").read_text().splitlines()]
    gold = [g for g in gold if split == "all" or g["split"] == split]
    scores: dict[str, dict[str, dict]] = {name: {} for name, _ in systems_read}
    for g in gold:
        for name, parses in systems_read:
            # a file a parse leaves out is scored as a failure, not dropped from the comparison
            scores[name][g["file"]] = score_file(g, parses.get(g["file"], {"error": "no parse"}))
    cats = sorted({g["category"] for g in gold})
    groups = {c: [g["file"] for g in gold if g["category"] == c] for c in cats}
    groups["ALL"] = [g["file"] for g in gold]
    groups["ALL_PDF"] = [g["file"] for g in gold if g["format"] == "pdf"]
    report: dict = {}
    names = [n for n, _ in systems_read]
    for grp, files in groups.items():
        report[grp] = {"n": len(files)}
        print(f"\n== {grp} (n={len(files)}, split={split})")
        print(
            f"{'metric':<20}"
            + "".join(f"{n:>14}" for n in names)
            + ("   paired diff vs " + names[0] + " [95% CI]" if len(names) > 1 else "")
        )
        for m in METRICS:
            vals = {
                n: [scores[n][f][m] for f in files if scores[n][f].get(m) is not None]
                for n in names
            }
            if not any(vals.values()):
                continue
            agg = (
                (lambda v: sum(v))
                if m in SUMMED
                else (lambda v: sum(v) / len(v) if v else float("nan"))
            )
            row = {n: agg(vals[n]) for n in names}
            line = f"{m:<20}" + "".join(f"{row[n]:>14.3f}" for n in names)
            report[grp][m] = row
            for n in names[1:]:
                paired = [
                    scores[n][f][m] - scores[names[0]][f][m]
                    for f in files
                    if scores[n][f].get(m) is not None and scores[names[0]][f].get(m) is not None
                ]
                if paired and m not in SUMMED:
                    b = paired_bootstrap(paired)
                    line += f"   {n}: {b.mean:+.3f} [{b.lb:+.3f}, {b.ub:+.3f}]"
                    report[grp].setdefault("diff", {}).setdefault(n, {})[m] = [b.mean, b.lb, b.ub]
            print(line)
    if out_json:
        out_json.write_text(json.dumps({"report": report, "files": scores}, indent=1))
    return report
