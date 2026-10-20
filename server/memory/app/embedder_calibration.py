"""Embedder calibration — percentile-based similarity thresholds derived
from the embedder's own cosine distribution at boot, instead of scalars.

Scalar gates are calibrated against one embedder's cosine distribution;
swap the embedder and the distribution shifts, so a fixed floor stops
filtering noise and starts filtering legitimate hits. A percentile of
the embedder's actual distribution is embedder-invariant.

At boot, probe three corpora and derive gates from the right one:
  - PARAPHRASE PAIRS → write-time gates (conflict, duplicate): the
    statement-vs-statement shape a racing write has.
  - QUESTION→FACT PAIRS → read-time trigger gates (rerank_skip,
    hyde_fallback, query_rewrite): a successful retrieval top-hit is a
    question→fact pair, which cosines well below paraphrase pairs.
  - DISTRACTOR PAIRS → filter gates (noise_floor, abstain): the 90th
    percentile marks where similarity stops being meaningful.

`LOOM_ADAPTIVE_THRESHOLDS` defaults to true. Fake/dev embedders and probe
failures return None, so gates fall through to the scalar settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any

from server.core import readiness
from server.llm import customer
from server.memory.store.rows import next_version
from server.settings import settings

log = logging.getLogger(__name__)


# Semantically-equivalent pairs — knowing one should let an agent
# answer the other.
PARAPHRASE_PAIRS: list[tuple[str, str]] = [
    ("Where does the user work?", "What is the user's employer?"),
    ("What's the user's job?", "Tell me about the user's career."),
    ("User loves coffee.", "The user enjoys drinking coffee."),
    ("User's favorite color is blue.", "Blue is the color the user prefers."),
    ("The user has a dog named Bowie.", "Bowie is the user's pet dog."),
    ("Deploys happen on Tuesdays.", "Tuesday is the day of the week for deployments."),
    ("Meeting at 3pm with Alice.", "Alice and the user are meeting at 3 in the afternoon."),
    ("User uses VSCode as their editor.", "VSCode is the IDE the user works in."),
    ("The book is 416 pages.", "It's a 416-page book."),
    ("User went to Tokyo last month.", "The user took a trip to Tokyo recently."),
    ("Currently working on the auth refactor.", "The auth refactor is the user's active project."),
    ("User is vegetarian.", "The user doesn't eat meat."),
    ("Daily standup at 10am.", "10am is when the team has standup every day."),
    ("Project X ships in Q4.", "Q4 is when Project X is scheduled to launch."),
    ("Loom is built on ClickHouse.", "ClickHouse is the database Loom uses."),
    ("User prefers dark mode.", "Dark theme is the user's preference."),
    ("Backups run nightly via Borg.", "Borg handles the nightly backups."),
    ("User lives in San Francisco.", "San Francisco is where the user is based."),
    ("The deploy failed on Friday.", "Friday's deployment was unsuccessful."),
    ("User joined the company in 2023.", "2023 is when the user started at the company."),
    ("Bowie weighs 32 pounds.", "Bowie's weight is 32 lbs."),
    ("User's flight is at 6am.", "6am is the departure time of the user's flight."),
    ("The hotel costs $200 per night.", "Hotel rate: $200/night."),
    ("User reads sci-fi books.", "Science fiction is the user's preferred genre."),
    ("Marriage anniversary is in June.", "June is the month of the user's wedding anniversary."),
    ("Team uses Slack for chat.", "Slack is the team's communication tool."),
    ("User's car is a Tesla Model 3.", "The user drives a Tesla Model 3."),
    ("Daily exercise is morning yoga.", "Yoga in the morning is the user's daily exercise."),
    ("Project budget is $50k.", "$50,000 is allocated for the project."),
    ("User commutes by bike.", "The user bikes to work."),
]


# Interrogative query vs the declarative fact that answers it — the
# shape a retrieval top-hit has when the right memory was found. Cosines
# well below the paraphrase distribution, so read-time trigger gates are
# derived from THIS corpus. Aggregational pairs (question implies
# computing over per-occurrence facts) are the lowest-cosine legit end.
QUESTION_FACT_PAIRS: list[tuple[str, str]] = [
    # direct lookup
    ("What city does the user live in?", "User lives in Rotterdam."),
    ("What is the user's dog called?", "User has a golden retriever named Pepper."),
    ("What kind of car does the user drive?", "User drives a silver station wagon."),
    ("What is the user's sister's name?", "User's sister Marieke lives in Utrecht."),
    ("What instrument does the user play?", "User has been taking cello lessons since September."),
    ("Where does the user buy groceries?", "User shops at the market hall on Saturdays."),
    # preference
    ("What is the user's favorite cuisine?", "User said Thai food is their favorite."),
    (
        "Does the user prefer window or aisle seats?",
        "User always books an aisle seat on long flights.",
    ),
    ("What kind of books does the user enjoy?", "User mostly reads historical fiction."),
    (
        "Which coffee order does the user usually get?",
        "User orders an oat-milk flat white most mornings.",
    ),
    (
        "What music does the user listen to while working?",
        "User listens to ambient playlists while coding.",
    ),
    ("Does the user like spicy food?", "User asked for extra chili oil with the noodles."),
    # temporal
    ("When did the user adopt their cat?", "User adopted a cat from the shelter in March."),
    ("When is the user's driving test?", "User's driving test is scheduled for next Thursday."),
    (
        "How long has the user lived in their apartment?",
        "User moved into the apartment two years ago.",
    ),
    (
        "When did the user last visit the dentist?",
        "User had a dental cleaning appointment last Tuesday.",
    ),
    ("What year did the user graduate?", "User finished their degree in 2019."),
    (
        "When does the user's gym membership renew?",
        "User's gym membership renews on the first of the month.",
    ),
    # aggregational
    ("How many plants does the user own?", "User bought a fern for the living room."),
    ("How much did the user spend on camping gear?", "User paid 40 euros for a camping stove."),
    (
        "How many times has the user been to the climbing gym this month?",
        "User went bouldering on Saturday morning.",
    ),
    (
        "How many days passed between the user's job interviews?",
        "User had a second interview on a Friday.",
    ),
    (
        "What is the total the user spent on birthday gifts?",
        "User bought a scarf as a birthday gift for their mother.",
    ),
    (
        "How many concerts did the user attend this year?",
        "User saw a jazz trio at the concert hall in May.",
    ),
]


# Unrelated statements — map the embedder's noise floor. The 90th
# percentile is the floor below which "matches" are coincidence.
DISTRACTOR_PAIRS: list[tuple[str, str]] = [
    ("Where does the user work?", "Bowie is a dog."),
    ("User loves coffee.", "Project X ships in Q4."),
    ("Daily standup at 10am.", "The book is 416 pages."),
    ("User went to Tokyo last month.", "Backups run nightly via Borg."),
    ("Marriage anniversary is in June.", "The deploy failed on Friday."),
    ("User uses VSCode as their editor.", "Bowie weighs 32 pounds."),
    ("User is vegetarian.", "Team uses Slack for chat."),
    ("User lives in San Francisco.", "Daily exercise is morning yoga."),
    ("The hotel costs $200 per night.", "Loom is built on ClickHouse."),
    ("User prefers dark mode.", "Project budget is $50k."),
    ("User commutes by bike.", "Currently working on the auth refactor."),
    ("User's car is a Tesla Model 3.", "User reads sci-fi books."),
    ("Meeting at 3pm with Alice.", "User joined the company in 2023."),
    ("Deploys happen on Tuesdays.", "User's flight is at 6am."),
    ("User's favorite color is blue.", "The deploy failed on Friday."),
    ("Loom is built on ClickHouse.", "User loves coffee."),
    ("Bowie weighs 32 pounds.", "Daily standup at 10am."),
    ("Daily exercise is morning yoga.", "User's car is a Tesla Model 3."),
    ("User's flight is at 6am.", "User reads sci-fi books."),
    ("The book is 416 pages.", "User is vegetarian."),
]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0 on zero-norm or mismatched-length inputs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _percentile(values: list[float], p: float) -> float:
    """`p`-th percentile, linear interpolation between ranks; 0 on empty."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    k = (len(sorted_v) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_v[int(k)]
    return sorted_v[f] + (sorted_v[c] - sorted_v[f]) * (k - f)


@dataclass(frozen=True)
class CalibrationResult:
    """Calibration output: distribution percentiles + derived thresholds."""

    similar_pairs_n: int
    distractor_pairs_n: int

    similar_p10: float
    similar_p25: float
    similar_p50: float
    similar_p75: float

    distractor_p50: float
    distractor_p90: float

    noise_floor: float
    rerank_skip_above: float
    hyde_fallback_threshold: float
    query_rewrite_threshold: float
    abstain_threshold: float
    conflict_threshold: float
    duplicate_threshold: float

    embedder_model: str
    is_fake: bool

    # Defaulted so rows hydrated from loom.namespace_calibration_result without
    # these columns still construct.
    question_fact_pairs_n: int = 0
    question_fact_p10: float = 0.0
    question_fact_p25: float = 0.0
    question_fact_p50: float = 0.0
    question_fact_p75: float = 0.0


def derive_thresholds(
    similar_cosines: list[float],
    question_fact_cosines: list[float],
    distractor_cosines: list[float],
    *,
    embedder_model: str,
) -> CalibrationResult:
    """Pure derivation: probe cosine distributions → CalibrationResult.

    Filter gates (noise_floor, abstain) anchor to the distractor
    distribution; read-time trigger gates (rerank_skip, hyde,
    query_rewrite) to question→fact; write-time gates (conflict,
    duplicate) to paraphrase.
    """
    sim_p10 = _percentile(similar_cosines, 10)
    sim_p25 = _percentile(similar_cosines, 25)
    sim_p50 = _percentile(similar_cosines, 50)
    sim_p75 = _percentile(similar_cosines, 75)
    qf_p10 = _percentile(question_fact_cosines, 10)
    qf_p25 = _percentile(question_fact_cosines, 25)
    qf_p50 = _percentile(question_fact_cosines, 50)
    qf_p75 = _percentile(question_fact_cosines, 75)
    dist_p50 = _percentile(distractor_cosines, 50)
    dist_p90 = _percentile(distractor_cosines, 90)

    # Small margins guard against a fluky distractor pair pulling
    # legitimate hits under the floor.
    noise_floor = dist_p90 + 0.02
    abstain_threshold = noise_floor + 0.05

    # Trigger gates can't fire below the floor (sub-floor hits already
    # dropped), so clamp each to floor + margin; the max() chain keeps
    # rewrite ≤ hyde ≤ rerank_skip even if question→fact degenerates
    # into the distractor range.
    floor_guard = noise_floor + 0.01
    query_rewrite_threshold = max(qf_p10, floor_guard)
    hyde_fallback_threshold = max(qf_p25, query_rewrite_threshold)
    rerank_skip_above = max(qf_p75, hyde_fallback_threshold)

    conflict_threshold = sim_p50
    duplicate_threshold = sim_p75

    return CalibrationResult(
        similar_pairs_n=len(similar_cosines),
        distractor_pairs_n=len(distractor_cosines),
        similar_p10=sim_p10,
        similar_p25=sim_p25,
        similar_p50=sim_p50,
        similar_p75=sim_p75,
        distractor_p50=dist_p50,
        distractor_p90=dist_p90,
        noise_floor=noise_floor,
        rerank_skip_above=rerank_skip_above,
        hyde_fallback_threshold=hyde_fallback_threshold,
        query_rewrite_threshold=query_rewrite_threshold,
        abstain_threshold=abstain_threshold,
        conflict_threshold=conflict_threshold,
        duplicate_threshold=duplicate_threshold,
        embedder_model=embedder_model,
        is_fake=False,
        question_fact_pairs_n=len(question_fact_cosines),
        question_fact_p10=qf_p10,
        question_fact_p25=qf_p25,
        question_fact_p50=qf_p50,
        question_fact_p75=qf_p75,
    )


async def calibrate_embedder(
    paraphrase_pairs: list[tuple[str, str]] | None = None,
    distractor_pairs: list[tuple[str, str]] | None = None,
    question_fact_pairs: list[tuple[str, str]] | None = None,
) -> CalibrationResult | None:
    """Probe the configured embedder and compute percentile-based
    thresholds. Returns None on probe failure (gates fall through to
    scalar settings).

    Each corpus arg: None → module-level default corpus; a custom list →
    per-namespace operator-shipped corpus. The operator corpus carries
    only similar/distractor pairs, so per-NS probes keep the module
    question→fact corpus — the question-vs-fact register gap is a
    property of the embedder, not the domain vocabulary.
    """
    from server.embedding.embedder import get_embedder

    para = paraphrase_pairs if paraphrase_pairs is not None else PARAPHRASE_PAIRS
    dist = distractor_pairs if distractor_pairs is not None else DISTRACTOR_PAIRS
    qf = question_fact_pairs if question_fact_pairs is not None else QUESTION_FACT_PAIRS
    if not para or not dist or not qf:
        log.warning(
            "calibration: empty corpus (paraphrase=%d, distractor=%d, question_fact=%d); skipping",
            len(para),
            len(dist),
            len(qf),
        )
        return None

    try:
        embedder = get_embedder()
    except Exception as e:  # noqa: BLE001 — no embedder: the scalar gates serve
        log.warning("calibration: embedder unavailable; skipping (%s)", e)
        return None

    is_fake = "fake" in embedder.model_name.lower()
    if is_fake:
        # Degenerate cosine distribution (every pair ~0); let the scalar
        # fallback handle it.
        log.info(
            "calibration: fake embedder (%s) — skipping percentile probe",
            embedder.model_name,
        )
        return None

    async def _embed_pair(a: str, b: str) -> float:
        # The recalibration worker's fixed pairs are the deployment's; code.calibrate
        # probes a namespace's corpus under its request's org, which wins.
        with customer.system():
            va, vb = await asyncio.gather(embedder.embed(a), embedder.embed(b))
        return _cosine(va, vb)

    try:
        similar_cosines = await asyncio.gather(*(_embed_pair(a, b) for a, b in para))
        question_fact_cosines = await asyncio.gather(*(_embed_pair(a, b) for a, b in qf))
        distractor_cosines = await asyncio.gather(*(_embed_pair(a, b) for a, b in dist))
    except Exception as e:  # noqa: BLE001 — a failed probe: the scalar gates serve
        log.warning("calibration: probe failed (%s); falling back to scalar settings", e)
        return None

    result = derive_thresholds(
        list(similar_cosines),
        list(question_fact_cosines),
        list(distractor_cosines),
        embedder_model=embedder.model_name,
    )
    log.info(
        "calibration: embedder=%s similar_p50=%.3f question_fact_p25=%.3f "
        "question_fact_p50=%.3f question_fact_p75=%.3f distractor_p90=%.3f "
        "→ noise_floor=%.3f rerank_skip=%.3f hyde_fallback=%.3f "
        "query_rewrite=%.3f abstain=%.3f",
        result.embedder_model,
        result.similar_p50,
        result.question_fact_p25,
        result.question_fact_p50,
        result.question_fact_p75,
        result.distractor_p90,
        result.noise_floor,
        result.rerank_skip_above,
        result.hyde_fallback_threshold,
        result.query_rewrite_threshold,
        result.abstain_threshold,
    )
    return result


# Cluster-wide singleton: adopted from loom.embedder_calibration (or probed
# when no row exists) at boot and kept in step with that row by the refresh
# loop. Used when no per-namespace override exists.
_calibration: CalibrationResult | None = None
# "shared" (adopted from, or published to, loom.embedder_calibration),
# "probe" (this process's own probe; no shared row readable) or "".
_calibration_source = ""
_calibration_hash = ""
# The shared-row read failed for a reason other than "no row" (table missing,
# CH down): one WARNING per process, then the local probe serves.
_shared_read_warned = False

# Per-namespace overrides, populated lazily by get_namespace_calibration.
_ns_overrides: dict[tuple[str, str], CalibrationResult | None] = {}
_ns_negative_at: dict[tuple[str, str], float] = {}
_ns_found_at: dict[tuple[str, str], float] = {}


def get_calibration() -> CalibrationResult | None:
    """Return the cluster-wide calibration, or None if not initialised."""
    return _calibration


def set_calibration(c: CalibrationResult | None, *, source: str = "probe") -> None:
    """Set the cluster-wide singleton (`source` says where it came from)."""
    global _calibration, _calibration_source, _calibration_hash
    _calibration = c
    _calibration_source = source if c is not None else ""
    _calibration_hash = calibration_hash(c) if c is not None else ""


def calibration_hash(c: CalibrationResult) -> str:
    """A short digest of every field: equal on two replicas iff they serve
    the same gates."""
    blob = json.dumps(asdict(c), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_calibration_hash() -> str:
    return _calibration_hash


def calibration_source() -> str:
    return _calibration_source


def calibration_applies() -> bool:
    """Whether adaptive gates are meant to serve: on, with a real embedder."""
    if not settings.adaptive_thresholds:
        return False
    return "fake" not in _served_model().lower()


def calibration_ready() -> bool:
    """Cheap and sync, for readiness: True when the gates this replica serves
    are the ones it should — adaptive off or a fake embedder (scalars are
    right), or a calibration loaded. False only while adaptive gates apply and
    neither the shared row nor a probe has produced one."""
    return _calibration is not None or not calibration_applies()


def readiness_check() -> readiness.CheckResult:
    return readiness.result(calibration_ready(), "similarity calibration not adopted")


def _served_model() -> str:
    try:
        from server.embedding.embedder import get_embedder

        return str(get_embedder().model_name)
    except Exception:  # noqa: BLE001 — no embedder: the configured name stands in
        return str(settings.embedding_model)


def shared_key(model: str) -> str:
    """The shared calibration's key: `model` and its embedding space's
    fingerprint, so the same label at another width or endpoint is probed, not
    adopted."""
    from server.core import embedding_space

    values = {**settings.model_dump(include=set(embedding_space.FIELDS)), "embedding_model": model}
    space = embedding_space.fingerprint(*embedding_space.public_identity(values))
    return f"{model}@{space[:16]}"


def calibration_from_payload(payload: str) -> CalibrationResult | None:
    """A CalibrationResult from a stored payload: unknown keys dropped, absent
    defaulted fields defaulted; None when a required field is missing."""
    try:
        data = json.loads(payload)
        known = {f.name for f in fields(CalibrationResult)}
        return CalibrationResult(**{k: v for k, v in data.items() if k in known})
    except (TypeError, ValueError):
        log.warning("calibration: unreadable shared payload; ignoring it")
        return None


async def _read_shared(model: str) -> tuple[CalibrationResult | None, bool]:
    """(the shared row's calibration or None, whether the table was readable)."""
    global _shared_read_warned
    from server.memory.store.calibration import fetch_global_calibration

    try:
        row = await fetch_global_calibration(shared_key(model))
    except Exception as e:  # noqa: BLE001 — no shared row readable: the local probe serves
        if not _shared_read_warned:
            _shared_read_warned = True
            log.warning(
                "calibration: loom.embedder_calibration unreadable (%s); this replica "
                "probes on its own and may serve gates other replicas do not",
                e,
            )
        return None, False
    if not row:
        return None, True
    c = calibration_from_payload(str(row["thresholds_json"]))
    if c is not None and c.embedder_model != model:
        return None, True
    return c, True


async def publish_calibration(c: CalibrationResult) -> bool:
    """Write `c` as the shared calibration for its embedder. False (logged)
    when the write fails; the caller keeps serving `c` either way."""
    from server.memory.store.calibration import write_global_calibration
    from server.observability.instance import instance_label

    try:
        await write_global_calibration(
            {
                "embedder_model": shared_key(c.embedder_model),
                "thresholds_json": json.dumps(asdict(c), sort_keys=True),
                "calibration_hash": calibration_hash(c),
                "calibrated_by": instance_label(),
                "version": next_version(),
            }
        )
    except Exception as e:  # noqa: BLE001 — the probed result still serves locally
        log.warning("calibration: could not publish the shared calibration (%s)", e)
        return False
    return True


async def adopt_or_probe() -> CalibrationResult | None:
    """Boot (and an embedder switch): adopt the shared calibration of the
    served embedder; probe only when there is none, and publish the probe
    when the table is readable and empty (the first replica to boot)."""
    if not calibration_applies():
        set_calibration(None)
        return None
    shared, readable = await _read_shared(_served_model())
    if shared is not None:
        set_calibration(shared, source="shared")
        return shared
    c = await calibrate_embedder()
    published = c is not None and readable and await publish_calibration(c)
    set_calibration(c, source="shared" if published else "probe")
    return c


async def refresh_shared() -> None:
    """One refresh tick: adopt the shared row when its hash differs from what
    this replica serves; with no row and no calibration (a failed boot probe),
    probe again. Never raises."""
    try:
        if not calibration_applies():
            return
        shared, readable = await _read_shared(_served_model())
        if shared is not None:
            if calibration_hash(shared) != _calibration_hash:
                log.info(
                    "calibration: adopting the shared calibration %s (was %s)",
                    calibration_hash(shared),
                    _calibration_hash or "none",
                )
                set_calibration(shared, source="shared")
            return
        if _calibration is None or (readable and _calibration_source == "probe"):
            # A replica whose boot probe failed retries; one that probed while
            # the table was unreadable publishes once it can.
            c = _calibration or await calibrate_embedder()
            published = c is not None and readable and await publish_calibration(c)
            set_calibration(c, source="shared" if published else "probe")
    except Exception as e:  # noqa: BLE001 — a refresh tick keeps the current calibration
        log.warning("calibration: refresh failed (%s); the current calibration stays", e)


_refresh_task: asyncio.Task[None] | None = None


def start_refresh_loop() -> asyncio.Task[None]:
    """Every replica re-reads the shared calibration each
    `calibration_refresh_seconds` (idempotent: one loop per process).
    Ticks are skipped while a model transition holds admission."""
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return _refresh_task

    async def _loop() -> None:
        from server.core.ch import set_query_tag
        from server.core.model_work_gate import get_model_work_gate

        set_query_tag("calibration.refresh")
        while True:
            await asyncio.sleep(max(5.0, float(settings.calibration_refresh_seconds)))
            if not get_model_work_gate().closed:
                await refresh_shared()

    _refresh_task = asyncio.get_running_loop().create_task(_loop(), name="loom-calibration-refresh")
    return _refresh_task


def stop_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None


def clear_ns_overrides() -> None:
    """Drop the per-NS cache so the next read fetches fresh from CH."""
    global _ns_overrides
    _ns_overrides = {}
    _ns_negative_at.clear()
    _ns_found_at.clear()


def namespace_calibration_row(org: str, namespace: str, c: CalibrationResult) -> dict[str, Any]:
    """One `loom.namespace_calibration_result` row (ReplacingMergeTree(version)) for
    `c`: what `get_namespace_calibration` reads back. The nanosecond version makes a
    re-run replace the prior result. `persist_namespace_calibration` writes it."""
    return {
        "org": org,
        "namespace": namespace,
        "embedder_model": c.embedder_model,
        "similar_p10": c.similar_p10,
        "similar_p25": c.similar_p25,
        "similar_p50": c.similar_p50,
        "similar_p75": c.similar_p75,
        "distractor_p50": c.distractor_p50,
        "distractor_p90": c.distractor_p90,
        "noise_floor": c.noise_floor,
        "rerank_skip_above": c.rerank_skip_above,
        "hyde_fallback_threshold": c.hyde_fallback_threshold,
        "query_rewrite_threshold": c.query_rewrite_threshold,
        "abstain_threshold": c.abstain_threshold,
        "conflict_threshold": c.conflict_threshold,
        "duplicate_threshold": c.duplicate_threshold,
        "similar_pairs_n": c.similar_pairs_n,
        "distractor_pairs_n": c.distractor_pairs_n,
        "calibrated_at": datetime.now(UTC),
        "version": next_version(),
    }


async def persist_namespace_calibration(org: str, namespace: str, c: CalibrationResult) -> None:
    """Write `c` as the namespace's calibration row through the client's attributed,
    retried insert, then drop the process cache so the next read fetches it."""
    from server.core.ch import get_client

    await get_client().insert(
        "loom.namespace_calibration_result", [namespace_calibration_row(org, namespace, c)]
    )
    clear_ns_overrides()


async def get_namespace_calibration(org: str, namespace: str) -> CalibrationResult | None:
    """Per-namespace calibration override from
    `loom.namespace_calibration_result`, else None (caller falls through
    to the cluster-wide `get_calibration()`). Cached per process for
    `memory_calibration_namespace_ttl_s`, so a row written on another replica serves here within it.
    """
    key = (org, namespace)
    if key in _ns_overrides:
        cached = _ns_overrides[key]
        if cached is not None:
            if (
                time.monotonic() - _ns_found_at.get(key, 0.0)
                < settings.memory_calibration_namespace_ttl_s
            ):
                return cached
        elif (
            time.monotonic() - _ns_negative_at.get(key, 0.0)
            < settings.memory_calibration_namespace_miss_ttl_s
        ):
            return None

    try:
        from server.core.ch import get_client

        ch = get_client()
        row = await ch.query_one(
            """
            SELECT
                embedder_model,
                similar_p10, similar_p25, similar_p50, similar_p75,
                distractor_p50, distractor_p90,
                noise_floor, rerank_skip_above, hyde_fallback_threshold,
                query_rewrite_threshold, abstain_threshold,
                conflict_threshold, duplicate_threshold,
                similar_pairs_n, distractor_pairs_n
            FROM loom.namespace_calibration_result FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — cache None: the global calibration serves
        # Table missing or CH down — cache None so we don't hammer CH
        # on every search.
        log.debug("ns calibration lookup failed (%s); caching None briefly", e)
        _ns_overrides[key] = None
        _ns_negative_at[key] = time.monotonic()
        return None

    if row is None:
        _ns_overrides[key] = None
        _ns_negative_at[key] = time.monotonic()
        return None

    result = CalibrationResult(
        similar_pairs_n=int(row["similar_pairs_n"]),
        distractor_pairs_n=int(row["distractor_pairs_n"]),
        similar_p10=float(row["similar_p10"]),
        similar_p25=float(row["similar_p25"]),
        similar_p50=float(row["similar_p50"]),
        similar_p75=float(row["similar_p75"]),
        distractor_p50=float(row["distractor_p50"]),
        distractor_p90=float(row["distractor_p90"]),
        noise_floor=float(row["noise_floor"]),
        rerank_skip_above=float(row["rerank_skip_above"]),
        hyde_fallback_threshold=float(row["hyde_fallback_threshold"]),
        query_rewrite_threshold=float(row["query_rewrite_threshold"]),
        abstain_threshold=float(row["abstain_threshold"]),
        conflict_threshold=float(row["conflict_threshold"]),
        duplicate_threshold=float(row["duplicate_threshold"]),
        embedder_model=str(row["embedder_model"]),
        is_fake=False,
    )
    _ns_overrides[key] = result
    _ns_found_at[key] = time.monotonic()
    return result


# Effective-threshold helpers. Resolution: per-namespace override →
# cluster-wide singleton → scalar fallback. Sync helpers take an
# optional pre-resolved CalibrationResult (hot path: resolve once per
# search, pass through); async helpers look up by (org, namespace)
# themselves (cold paths like server/memory/app/conflicts.py).


async def resolve_calibration(org: str, namespace: str) -> CalibrationResult | None:
    """Per-NS override if present, else cluster-wide singleton. Async
    because the per-NS lookup hits CH (cached after first read)."""
    ns_cal = await get_namespace_calibration(org, namespace)
    if ns_cal is not None:
        return ns_cal
    return _calibration


def effective_noise_floor(
    scalar_fallback: float, calibration: CalibrationResult | None = None
) -> float:
    c = calibration if calibration is not None else _calibration
    return c.noise_floor if c is not None else scalar_fallback


def effective_hyde_fallback_threshold(
    scalar_fallback: float, calibration: CalibrationResult | None = None
) -> float:
    c = calibration if calibration is not None else _calibration
    return c.hyde_fallback_threshold if c is not None else scalar_fallback


def effective_abstain_threshold(
    scalar_fallback: float, calibration: CalibrationResult | None = None
) -> float:
    c = calibration if calibration is not None else _calibration
    return c.abstain_threshold if c is not None else scalar_fallback


async def effective_conflict_threshold_for_ns(
    scalar_fallback: float, org: str, namespace: str
) -> float:
    """Async per-NS variant for the write-time conflict gate."""
    c = await resolve_calibration(org, namespace)
    return c.conflict_threshold if c is not None else scalar_fallback


async def effective_duplicate_threshold_for_ns(
    scalar_fallback: float, org: str, namespace: str
) -> float:
    """Async per-NS variant for the write-time duplicate gate."""
    c = await resolve_calibration(org, namespace)
    return c.duplicate_threshold if c is not None else scalar_fallback
