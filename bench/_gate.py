"""The accuracy gate every judged bench reads its verdict from.

A judged accuracy is two random draws on top of the thing measured: the
reader re-rolls at temperature 0 (8.5% of LoCoMo labels flipped between two
identical runs with identical top-10 keys; the judge flipped 0 of 39
byte-identical answers), and the retrieval inside synthesis re-decomposes
(43% of top-key lists differed). Two identical single-trial runs of 164
questions differed by ±3–4 points, so single-trial deltas of that size
(−0.31, −0.40, −1.18 pp) are inside the band, not results. Hosted judges
show the same instability (about 13.6% mean flip; 11 trials recover the
50-trial majority at 95%), and a judge's TPR/FPR measured on a labelled set
can correct the test threshold.

This module is the one place the rule lives, so `locomo_qa`, `longmemeval`,
`membench`, `route_replay` and `relied_on_sweep` all read the same verdict:

* `--repeats N` — each item is asked N times on the same retrieval; the item
  keeps the MAJORITY of its judged labels (tie = WRONG, the conservative
  side; judged runs outvote errors). `flip_rate` is the share of items whose
  N labels disagreed — the bench's own noise reading, printed beside the
  delta it qualifies.
* `paired_gate` — the paired, cluster-resampled bootstrap (`bench.bootstrap`)
  over the majority labels of two arms, and a verdict:
    pass        the 95% lower bound clears `min_delta_pp`
    fail        the 95% upper bound is below `min_delta_pp`
    unresolved  the interval straddles the threshold — inside the measured
                null band (`|delta| < band_pp`) it is the noise floor, not a
                null result; outside it the run is under-powered (more n).
* `JudgeCalibration` — the judge's TPR/FPR measured once on known-correct and
  known-wrong answers (`bench/judge_calibration.py`). Observed accuracy is
  `TPR·p + FPR·(1−p)`, so a paired delta is attenuated by Youden's
  `J = TPR − FPR`; the corrected delta is `Δ/J` and its interval carries the
  calibration set's own sampling variance (delta method), so the gate uses a
  variance-corrected threshold.

Pure Python, deterministic under `seed`, no numpy; safe to import from a
bench run as a script (`python bench/x.py`) or as a module.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from bench.bootstrap import BootResult, paired_bootstrap
except ModuleNotFoundError:  # `python bench/<runner>.py` puts bench/ on sys.path
    from bootstrap import BootResult, paired_bootstrap  # type: ignore[no-redef]

JUDGED = ("CORRECT", "WRONG")

# The measured null band: two identical single-trial runs of the ask reader
# differed by up to 4.9 points at n=164 (±3–4 typical).
# `--gate-band` overrides it with a band measured on the fixture at hand.
DEFAULT_BAND_PP = 4.0
GATE_REPEATS = 3


# ---------------------------------------------------------------- labels


def normalize_label(label: Any) -> str:
    """Any bench's per-run verdict as CORRECT / WRONG / ERROR: booleans,
    `correct`/`wrong`/`abstained` (route_replay), `CORRECT`/`WRONG`."""
    if isinstance(label, bool):
        return "CORRECT" if label else "WRONG"
    s = str(label or "").strip().upper()
    if s in JUDGED:
        return s
    if s in ("TRUE", "1", "YES"):
        return "CORRECT"
    if s in ("FALSE", "0", "NO", "ABSTAINED", "ABSTAIN"):
        return "WRONG"
    return "ERROR"


def majority_label(labels: Sequence[str]) -> str:
    """The label an item keeps after N repeats: CORRECT only when more than
    half the judged runs said so (a tie is WRONG — the conservative side);
    ERROR only when every run errored. Judged runs outvote errors."""
    judged = [normalize_label(lb) for lb in labels]
    judged = [lb for lb in judged if lb in JUDGED]
    if not judged:
        return "ERROR" if labels else "WRONG"
    correct = sum(lb == "CORRECT" for lb in judged)
    return "CORRECT" if 2 * correct > len(judged) else "WRONG"


def is_unstable(labels: Sequence[str]) -> bool:
    """True when the item's repeated runs disagreed (both judged labels seen)."""
    seen = {normalize_label(lb) for lb in labels} & set(JUDGED)
    return len(seen) > 1


def agreement(labels: Sequence[str]) -> float:
    """Share of runs that agree with the majority (1.0 for a single run)."""
    if not labels:
        return 1.0
    maj = majority_label(labels)
    return round(sum(normalize_label(lb) == maj for lb in labels) / len(labels), 3)


def flip_rate(label_lists: Iterable[Sequence[str]]) -> float | None:
    """Share of repeated items (≥ 2 judged labels) whose labels disagreed;
    None when nothing was repeated. This is the per-item flip rate the gate
    prints beside the delta: 8.5% single-trial on LoCoMo's ask reader."""
    repeated = 0
    flipped = 0
    for labels in label_lists:
        judged = [lb for lb in (normalize_label(x) for x in labels) if lb in JUDGED]
        if len(judged) < 2:
            continue
        repeated += 1
        flipped += is_unstable(judged)
    return round(flipped / repeated, 4) if repeated else None


def identical_share(values: Sequence[Any]) -> float:
    """Share of repeats whose value equals the first run's (1.0 for one run):
    the retrieval-pin check — with `top_keys` it says whether N repeats saw
    the same window, so a flip is the reader's and not the retrieval's."""
    if not values:
        return 1.0
    first = values[0]
    return round(sum(v == first for v in values) / len(values), 3)


def split_labels(cell: str, sep: str = "|") -> list[str]:
    """`repeat_labels` CSV cell (`CORRECT|WRONG|CORRECT`) back to a list;
    an empty cell is no repeats."""
    return [p for p in (cell or "").split(sep) if p]


# ---------------------------------------------------------------- calibration


@dataclass(frozen=True)
class JudgeCalibration:
    """Judge TPR / FPR on known-correct / known-wrong answers (n_pos / n_neg
    items), from `bench/judge_calibration.py`."""

    tpr: float
    fpr: float
    n_pos: int
    n_neg: int
    judge_model: str = ""

    @property
    def youden(self) -> float:
        return self.tpr - self.fpr

    @property
    def variance_tpr(self) -> float:
        return self.tpr * (1.0 - self.tpr) / self.n_pos if self.n_pos else 0.0

    @property
    def variance_fpr(self) -> float:
        return self.fpr * (1.0 - self.fpr) / self.n_neg if self.n_neg else 0.0

    def correct_accuracy(self, observed: float) -> float:
        """True accuracy behind an observed judged accuracy (0–1):
        `(p_obs − FPR) / (TPR − FPR)`, clipped to [0, 1]."""
        j = self.youden
        if j <= 0:
            return observed
        return max(0.0, min(1.0, (observed - self.fpr) / j))

    def correct_delta(
        self, delta: float, lb: float, ub: float, *, z: float = 1.959964
    ) -> tuple[float, float, float]:
        """A paired delta and its 95% interval (same unit in and out), de-
        attenuated by Youden's J and widened by the calibration set's own
        sampling variance (delta method on Δ/J):
            Var(Δ/J) ≈ Var(Δ)/J² + Δ²·(Var(TPR) + Var(FPR))/J⁴
        Var(Δ) is read back from the bootstrap interval's half-width. A
        judge with J ≤ 0 carries no information; the inputs come back as is."""
        j = self.youden
        if j <= 0:
            return delta, lb, ub
        half = (ub - lb) / 2.0
        var_obs = (half / z) ** 2
        var_c = var_obs / (j * j) + (delta * delta) * (self.variance_tpr + self.variance_fpr) / (
            j**4
        )
        d = delta / j
        w = z * math.sqrt(max(0.0, var_c))
        return d, d - w, d + w

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "youden": round(self.youden, 4)}


def parse_calibration(spec: str) -> JudgeCalibration | None:
    """`--judge-calibration`: '' = off; a JSON file written by
    `bench/judge_calibration.py` ({tpr, fpr, n_pos, n_neg, judge_model});
    or an inline `tpr,fpr,n_pos,n_neg`."""
    s = (spec or "").strip()
    if not s:
        return None
    p = Path(s)
    if p.exists():
        obj = json.loads(p.read_text())
        return JudgeCalibration(
            tpr=float(obj["tpr"]),
            fpr=float(obj["fpr"]),
            n_pos=int(obj["n_pos"]),
            n_neg=int(obj["n_neg"]),
            judge_model=str(obj.get("judge_model", "")),
        )
    parts = [x.strip() for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError("--judge-calibration wants a JSON path or tpr,fpr,n_pos,n_neg")
    return JudgeCalibration(float(parts[0]), float(parts[1]), int(parts[2]), int(parts[3]))


# ---------------------------------------------------------------- the gate


@dataclass(frozen=True)
class GateResult:
    n: int
    clusters: int
    repeats_a: int
    repeats_b: int
    accuracy_a: float  # 0–1, majority labels
    accuracy_b: float
    delta_pp: float  # B − A, percentage points
    lb_pp: float
    ub_pp: float
    wins: int
    ties: int
    losses: int
    flip_rate_a: float | None  # share of repeated items whose labels disagreed
    flip_rate_b: float | None
    band_pp: float
    min_delta_pp: float
    verdict: str  # pass | fail | unresolved
    reason: str
    calibration: dict[str, Any] | None = None
    # Items unstable WITHIN either arm's repeats (their labels disagreed), and
    # the same pair over the remaining stable items. Half of the
    # discordance left under a 3-trial majority sat on items whose six
    # labels split near 50/50 — a vote cannot settle a fair coin; flagging
    # them takes a same-config pair from 3.0% to 1.4% discordance.
    n_unstable: int = 0
    stable: dict[str, Any] | None = None
    # Provenance: the resampling that produced the interval.
    resamples: int = 0
    seed: int = 0

    @property
    def straddles(self) -> bool:
        return self.lb_pp <= self.min_delta_pp <= self.ub_pp

    @property
    def discordance(self) -> float | None:
        """Share of paired items whose (majority) labels differ between the
        arms — on a same-config null pair this is the label's run-to-run flip
        probability: 8.5% single-trial on LoCoMo's ask reader, the number a
        3-trial majority is meant to take to ~2%. `flip_rate_*` is the other
        reading — disagreement WITHIN an arm's repeats (≈ 3p for small p)."""
        return round((self.wins + self.losses) / self.n, 4) if self.n else None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "discordance": self.discordance}

    def lines(self) -> list[str]:
        fr_a = "-" if self.flip_rate_a is None else f"{100 * self.flip_rate_a:.1f}%"
        fr_b = "-" if self.flip_rate_b is None else f"{100 * self.flip_rate_b:.1f}%"
        out = [
            f"gate: n={self.n} clusters={self.clusters} repeats A={self.repeats_a} "
            f"B={self.repeats_b} (majority labels)",
            f"  accuracy A {100 * self.accuracy_a:.2f}  B {100 * self.accuracy_b:.2f}  "
            f"delta {self.delta_pp:+.2f} pp  95% CI [{self.lb_pp:+.2f}, {self.ub_pp:+.2f}]  "
            f"W/T/L {self.wins}/{self.ties}/{self.losses}",
            f"  within-run flip rate A {fr_a}  B {fr_b}   between-arm discordance "
            f"{'-' if self.discordance is None else f'{100 * self.discordance:.1f}%'}   "
            f"band ±{self.band_pp:.1f} pp   threshold {self.min_delta_pp:+.2f} pp",
        ]
        if self.calibration:
            c = self.calibration
            out.append(
                f"  judge-calibrated (TPR {c['tpr']:.3f} FPR {c['fpr']:.3f} J {c['youden']:.3f}): "
                f"delta {c['delta_pp']:+.2f} pp  CI [{c['lb_pp']:+.2f}, {c['ub_pp']:+.2f}]"
            )
        if self.stable:
            st = self.stable
            disc = st.get("discordance")
            out.append(
                f"  stable items only (excl. {self.n_unstable} unstable within-run): n={st['n']} "
                f"delta {st['delta_pp']:+.2f} pp  CI [{st['lb_pp']:+.2f}, {st['ub_pp']:+.2f}]  "
                f"discordance {'-' if disc is None else f'{100 * disc:.1f}%'}  {st['verdict']}"
            )
        out.append(f"  VERDICT {self.verdict.upper()} — {self.reason}")
        return out


def verdict_for(
    delta_pp: float, lb_pp: float, ub_pp: float, *, band_pp: float, min_delta_pp: float = 0.0
) -> tuple[str, str]:
    """pass / fail / unresolved with the reason the record quotes."""
    if lb_pp > min_delta_pp:
        return "pass", f"lower bound {lb_pp:+.2f} clears {min_delta_pp:+.2f}"
    if ub_pp < min_delta_pp:
        return "fail", f"upper bound {ub_pp:+.2f} is below {min_delta_pp:+.2f}"
    if abs(delta_pp - min_delta_pp) < band_pp:
        return (
            "unresolved",
            f"interval straddles {min_delta_pp:+.2f} and |delta| {abs(delta_pp):.2f} is inside "
            f"the ±{band_pp:.1f} pp null band — the noise floor, not a null result",
        )
    return (
        "unresolved",
        f"interval straddles {min_delta_pp:+.2f} with |delta| {abs(delta_pp):.2f} outside the "
        f"±{band_pp:.1f} pp band — under-powered; raise n",
    )


def paired_gate(
    a: Mapping[Any, Sequence[str]],
    b: Mapping[Any, Sequence[str]],
    *,
    clusters: Mapping[Any, str] | None = None,
    band_pp: float = DEFAULT_BAND_PP,
    min_delta_pp: float = 0.0,
    calibration: JudgeCalibration | None = None,
    resamples: int = 10_000,
    seed: int = 7,
    _stable_only: bool = False,
) -> GateResult:
    """Paired gate of arm B against arm A over the items both hold.

    `a[key]` / `b[key]` are the item's per-run labels (one entry for a
    single-trial run); the item enters as the MAJORITY of its judged labels.
    Items that errored in either arm's majority are dropped from the pair
    (an ERROR majority means every run errored). `clusters[key]` groups
    correlated items (a conversation, a question type) for the resampling."""
    keys = [k for k in a if k in b]
    deltas: list[float] = []
    cl: list[str] = []
    ca_total = 0.0
    cb_total = 0.0
    for k in keys:
        ma, mb = majority_label(list(a[k])), majority_label(list(b[k]))
        if ma == "ERROR" or mb == "ERROR":
            continue
        ca, cb = float(ma == "CORRECT"), float(mb == "CORRECT")
        ca_total += ca
        cb_total += cb
        deltas.append(cb - ca)
        cl.append(str(clusters.get(k, k)) if clusters is not None else str(k))
    n = len(deltas)
    boot: BootResult = (
        paired_bootstrap(deltas, cl, resamples=resamples, seed=seed)
        if deltas
        else BootResult(0, 0, 0.0, 0.0, 0.0, 0, 0, 0, 0)
    )
    # The verdict reads the unrounded interval; rounding is for the record only.
    raw = (100.0 * boot.mean, 100.0 * boot.lb, 100.0 * boot.ub)
    cal_block: dict[str, Any] | None = None
    v_delta, v_lb, v_ub = raw
    if calibration is not None:
        v_delta, v_lb, v_ub = calibration.correct_delta(*raw)
        cal_block = {
            **calibration.as_dict(),
            "delta_pp": round(v_delta, 2),
            "lb_pp": round(v_lb, 2),
            "ub_pp": round(v_ub, 2),
        }
    verdict, reason = verdict_for(v_delta, v_lb, v_ub, band_pp=band_pp, min_delta_pp=min_delta_pp)
    if calibration is not None:
        reason = "judge-calibrated: " + reason
    unstable = [k for k in keys if is_unstable(list(a[k])) or is_unstable(list(b[k]))]
    stable_block: dict[str, Any] | None = None
    if unstable and not _stable_only:
        drop = set(unstable)
        stable_block = paired_gate(
            {k: a[k] for k in keys if k not in drop},
            {k: b[k] for k in keys if k not in drop},
            clusters=clusters,
            band_pp=band_pp,
            min_delta_pp=min_delta_pp,
            calibration=calibration,
            resamples=resamples,
            seed=seed,
            _stable_only=True,
        ).as_dict()
    return GateResult(
        n=n,
        clusters=boot.clusters,
        repeats_a=max((len(v) for v in a.values()), default=0),
        repeats_b=max((len(v) for v in b.values()), default=0),
        accuracy_a=round(ca_total / n, 4) if n else 0.0,
        accuracy_b=round(cb_total / n, 4) if n else 0.0,
        delta_pp=round(raw[0], 2),
        lb_pp=round(raw[1], 2),
        ub_pp=round(raw[2], 2),
        wins=boot.wins,
        ties=boot.ties,
        losses=boot.losses,
        flip_rate_a=flip_rate(a[k] for k in keys),
        flip_rate_b=flip_rate(b[k] for k in keys),
        band_pp=float(band_pp),
        min_delta_pp=float(min_delta_pp),
        verdict=verdict,
        reason=reason,
        calibration=cal_block,
        n_unstable=len(unstable),
        stable=stable_block,
        resamples=boot.resamples,
        seed=seed,
    )


def band_from_null(result: GateResult) -> float:
    """The null band a same-config pair measured: the wider side of its
    interval, in pp — what `--gate-band` takes on the next real arm."""
    return round(max(abs(result.lb_pp), abs(result.ub_pp)), 2)


# ---------------------------------------------------------------- measurement reads

# A benchmark read is an EVALUATION read. A crediting read resolves a memory key
# to all of its rows (one bench search of 25 hits can write ~550 recall events),
# and each judged pass would reinforce the fact plane the next pass ranks
# against, moving results between passes with no code change. So every bench
# body carries these unless the run opts back in with `--credit`.
SEARCH_MEASUREMENT_FIELDS: dict[str, Any] = {"auto_touch": False}
ASK_MEASUREMENT_FIELDS: dict[str, Any] = {"reinforce": False}


def measurement_fields(op: str, *, credit: bool = False) -> dict[str, Any]:
    """The evaluation-read fields for a `memory.search` / `memory.ask` body:
    `{"auto_touch": False}` / `{"reinforce": False}`; empty when the run
    opted in to crediting (`--credit`). Merge them FIRST so an arm's own
    `--ask-json` / `--search-json` still wins."""
    if credit:
        return {}
    if op in ("search", "memory.search"):
        return dict(SEARCH_MEASUREMENT_FIELDS)
    if op in ("ask", "memory.ask"):
        return dict(ASK_MEASUREMENT_FIELDS)
    return {}


# ---------------------------------------------------------------- CLI glue


def add_gate_arguments(
    p: argparse.ArgumentParser, *, repeats: bool = True, prefix: str = "gate"
) -> None:
    """The shared flags. `repeats=False` for a bench that already has
    `--repeats` with its own help text."""
    if repeats:
        p.add_argument(
            "--repeats",
            type=int,
            default=1,
            help="ask+judge each item N times on the same retrieval and keep the per-item "
            "MAJORITY label (tie = WRONG); reports the per-item flip rate beside the "
            f"delta. Gate runs use {GATE_REPEATS}; default 1.",
        )
    p.add_argument(
        f"--{prefix}",
        action="store_true",
        help="print delta, 95%% paired clustered bootstrap CI over majority labels, per-item "
        "flip rate and a verdict pass|fail|unresolved (unresolved = the CI straddles the "
        "threshold; inside the null band it is the noise floor, not a null)",
    )
    p.add_argument(
        f"--{prefix}-band",
        type=float,
        default=DEFAULT_BAND_PP,
        help=f"the measured null band in pp (default {DEFAULT_BAND_PP}: two identical "
        "single-trial LoCoMo ask runs at n=164); pass the band a null pair measured",
    )
    p.add_argument(
        f"--{prefix}-min-delta",
        type=float,
        default=0.0,
        help="pp threshold the lower bound must clear (0 = any gain; a non-inferiority gate "
        "passes -0.5 and reads pass when the arm is not worse than that)",
    )
    p.add_argument(
        "--credit",
        action="store_true",
        help="opt back in to usage crediting: send the server's defaults (memory.search "
        "auto_touch=true, memory.ask reinforce=true) instead of the evaluation-read "
        "fields; off = every bench read leaves the reinforcement counts alone",
    )
    p.add_argument(
        "--judge-calibration",
        default="",
        help="judge TPR/FPR from bench/judge_calibration.py: a JSON path or "
        "'tpr,fpr,n_pos,n_neg'; the gate's delta and interval are de-attenuated by "
        "Youden's J and widened by the calibration variance. '' = off",
    )


def gate_from_args(
    args: argparse.Namespace,
    a: Mapping[Any, Sequence[str]],
    b: Mapping[Any, Sequence[str]],
    *,
    clusters: Mapping[Any, str] | None = None,
    prefix: str = "gate",
    seed: int = 7,
) -> GateResult:
    """`paired_gate` with the shared flags read off the parsed namespace."""
    band = float(getattr(args, f"{prefix}_band", DEFAULT_BAND_PP) or DEFAULT_BAND_PP)
    min_delta = float(getattr(args, f"{prefix}_min_delta", 0.0) or 0.0)
    cal = parse_calibration(str(getattr(args, "judge_calibration", "") or ""))
    return paired_gate(
        a, b, clusters=clusters, band_pp=band, min_delta_pp=min_delta, calibration=cal, seed=seed
    )


def _row_key(r: Mapping[str, Any], keys: Sequence[str]) -> Any:
    if len(keys) == 1:
        return str(r.get(keys[0], ""))
    return tuple(str(r.get(x, "")) for x in keys)


def labels_by_key(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str | Sequence[str],
    label: str = "label",
    repeat_labels: str = "repeat_labels",
) -> dict[Any, list[str]]:
    """CSV/JSON rows → {item key: per-run labels}: the `repeat_labels` cell
    when the row has one, else the single `label`."""
    keys = [key] if isinstance(key, str) else list(key)
    out: dict[Any, list[str]] = {}
    for r in rows:
        k: Any = _row_key(r, keys)
        reps = split_labels(str(r.get(repeat_labels, "") or ""))
        out[k] = reps if reps else [normalize_label(r.get(label, ""))]
    return out


def group_by(
    rows: Iterable[Mapping[str, Any]], *, key: str | Sequence[str], field: str
) -> dict[Any, str]:
    """{item key: cluster label} from a row field (conversation, question type)."""
    keys = [key] if isinstance(key, str) else list(key)
    out: dict[Any, str] = {}
    for r in rows:
        k: Any = _row_key(r, keys)
        out[k] = str(r.get(field, "") or "")
    return out


__all__ = [
    "ASK_MEASUREMENT_FIELDS",
    "DEFAULT_BAND_PP",
    "GATE_REPEATS",
    "GateResult",
    "JudgeCalibration",
    "SEARCH_MEASUREMENT_FIELDS",
    "add_gate_arguments",
    "agreement",
    "band_from_null",
    "flip_rate",
    "gate_from_args",
    "group_by",
    "identical_share",
    "is_unstable",
    "labels_by_key",
    "majority_label",
    "measurement_fields",
    "normalize_label",
    "paired_gate",
    "parse_calibration",
    "split_labels",
    "verdict_for",
]
