"""Data-only validation, pending output reuse, and dependency-safe retirement."""

from __future__ import annotations

import posixpath
import re
import subprocess
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from scripts.i18n import refresh
from scripts.i18n import translate as t

MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_PENDING_BYTES = 256 * 1024 * 1024


def source_path(source: str) -> bool:
    parts = PurePosixPath(source).parts
    return (
        bool(parts)
        and not re.search(r"[\x00-\x1f\x7f\\]", source)
        and not source.startswith("/")
        and ".." not in parts
        and str(PurePosixPath(source)) == source
        and (source == "README.md" or (parts[0] == "docs" and source.endswith(".md")))
    )


def all_files() -> list[str]:
    return sorted(
        p.relative_to(t.ROOT).as_posix()
        for p in (t.ROOT / t.OUT_DIR).rglob("*")
        if p.is_file() or p.is_symlink()
    )


def publication_path(path: str) -> str:
    return path.replace("i18n/.pending/", "i18n/", 1) if path.startswith("i18n/.pending/") else path


def checkpoint_path(path: str) -> str:
    return "i18n/.pending/" + path.removeprefix("i18n/")


def output_files() -> list[str]:
    return [p for p in all_files() if not p.startswith("i18n/.pending/")]


def checkpoint_files() -> list[str]:
    return [p for p in all_files() if p.startswith("i18n/.pending/") and p != refresh.PATH]


def regular(path: str) -> bool:
    file = t.ROOT / path
    return (
        file.resolve().is_relative_to(t.ROOT.resolve())
        and file.is_file()
        and not any(p.is_symlink() for p in (file, *file.parents))
    )


def owner(path: str, text: str) -> tuple[str, t.Language] | None:
    path = publication_path(path)
    stamp = t.STAMP.match(text)
    if not stamp or not source_path(stamp[1]):
        return None
    for lang in t.LANGUAGES:
        if path == t.target_path(lang.code, stamp[1]):
            return stamp[1], lang
    return None


def strip_language_line(text: str) -> str:
    return t.LANGUAGE_LINE.sub("<!-- languages -->", text)


def canonical_links(text: str, current: str) -> str:
    """Compare translated and English fallback links as the same source target."""

    def fix(prose: str) -> str:
        def replace(match: re.Match[str]) -> str:
            prefix, link = match.groups()
            if re.match(r"[a-z]+:|#|/", link):
                return match[0]
            path, _, anchor = link.partition("#")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(current), unquote(path)))
            parts = resolved.split("/", 2)
            if (
                len(parts) == 3
                and parts[0] == t.OUT_DIR
                and parts[1] in {lg.code for lg in t.LANGUAGES}
            ):
                resolved = parts[2]
            image = prefix == 'src="' or (prefix == "](" and t._is_image(prose, match.start()))
            suffix = f"#{anchor}" if anchor else ""
            return (
                f"{prefix}{t.REPO_URL}/{'raw' if image else 'blob'}/main/{quote(resolved)}{suffix}"
            )

        return t.LINK.sub(replace, prose)

    return t.in_prose(text, fix)


def candidate_problems(path: str, text: str, *, current: bool = True) -> list[str]:
    path = publication_path(path)
    owned = owner(path, text)
    if owned is None:
        return [f"{path}: not a generated page with an exact source/language mapping"]
    source, lang = owned
    stamp = t.STAMP.match(text)
    assert stamp
    model = t.os.environ.get("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    if current and (source not in t.sources() or stamp[2] != t.blob(source) or stamp[3] != model):
        return [f"{path}: pending output is stale"]
    expected_header = t.header(source, lang, stamp[3], stamp[2])
    if not text.startswith(expected_header):
        return [f"{path}: stamp or banner differs"]
    if not current:
        return []
    english = strip_language_line((t.ROOT / source).read_text())
    english = "\n".join(t.english_requests(english))
    body = strip_language_line(text[len(expected_header) :])
    english = canonical_links(english, source)
    body = canonical_links(body, path)
    problems = t.differences(english, body, source_path=source)
    if t.chunks.table_shape(english) != t.chunks.table_shape(body):
        problems.append("table row/cell layout differs after request assembly")
    return [f"{path}: {problem}" for problem in problems]


def body_start(path: str, text: str) -> int:
    owned = owner(path, text)
    stamp = t.STAMP.match(text)
    if owned and stamp:
        header = t.header(owned[0], owned[1], stamp[3], stamp[2])
        if text.startswith(header):
            return len(header)
    return 0


def english_fallback(link: str) -> tuple[str, str] | None:
    prefix = f"{t.REPO_URL}/blob/main/"
    if not link.startswith(prefix):
        return None
    url = urlsplit(link)
    source = unquote(url.path.removeprefix(urlsplit(prefix).path))
    return (source, url.fragment) if source_path(source) else None


def english_live(
    source: str,
    anchor: str,
    inventory: frozenset[str],
    *,
    _bodies: dict[str, tuple[str, str]] | None = None,
) -> bool:
    if source not in inventory or not regular(source):
        return False
    # Recheck the file and its bytes per link; reuse only the derived parse within a pass.
    text = (t.ROOT / source).read_text()
    cached = _bodies.get(source) if _bodies is not None else None
    if cached is None or cached[0] != text:
        body = "\n".join(t.english_requests(text))
        if _bodies is not None:
            _bodies[source] = (text, body)
    else:
        body = cached[1]
    return not anchor or f'<a id="{unquote(anchor)}"></a>' in body


def link_failures(extra: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    pages = {
        p: (t.ROOT / p).read_text() for p in output_files() if regular(p) and p.endswith(".md")
    }
    pages.update(extra or {})
    inventory = frozenset(t.sources())
    english_bodies: dict[str, tuple[str, str]] = {}
    problems = []
    for path, text in pages.items():
        for match in t.prose_links(text[body_start(path, text) :]):
            link, _, anchor = match[2].partition("#")
            fallback = english_fallback(match[2])
            if fallback:
                source, fragment = fallback
                if not english_live(source, fragment, inventory, _bodies=english_bodies):
                    problems.append(
                        (
                            path,
                            source,
                            f"{path} links to {match[2]}, "
                            "whose English destination or anchor is missing",
                        )
                    )
                continue
            if re.match(r"[a-z]+:", link):
                continue
            target = (
                posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(link)))
                if link
                else path
            )
            if target not in pages and not regular(target):
                problems.append((path, target, f"{path} links to {match[2]}, which does not exist"))
            elif anchor:
                body = pages[target] if target in pages else (t.ROOT / target).read_text()
                if f'<a id="{unquote(anchor)}"></a>' not in body:
                    problems.append(
                        (path, target, f"{path} links to {match[2]}, which has no such anchor")
                    )
    return problems


def repair_english_fallbacks(codes: list[str] | None = None) -> None:
    """Keep deleted English body links on an owned published target that retains the anchor."""
    inventory = frozenset(t.sources())
    english_bodies: dict[str, tuple[str, str]] = {}
    for path in output_files():
        if not regular(path):
            continue
        text = (t.ROOT / path).read_text()
        start = body_start(path, text)
        owned = owner(path, text)
        if not start or not owned or (codes is not None and owned[1].code not in codes):
            continue

        def fix(prose: str, path: str = path, language: str = owned[1].code) -> str:
            def replace(match: re.Match[str]) -> str:
                fallback = english_fallback(match[2])
                if not fallback or english_live(*fallback, inventory, _bodies=english_bodies):
                    return match[0]
                source, anchor = fallback
                target = t.target_path(language, source)
                if not regular(target):
                    return match[0]
                target_text = (t.ROOT / target).read_text()
                if owner(target, target_text) is None or (
                    anchor and f'<a id="{unquote(anchor)}"></a>' not in target_text
                ):
                    return match[0]
                relative = quote(posixpath.relpath(target, posixpath.dirname(path)))
                return f"{match[1]}{relative}{'#' + anchor if anchor else ''}"

            return t.LINK.sub(replace, prose)

        repaired = text[:start] + t.in_prose(text[start:], fix)
        if repaired != text:
            (t.ROOT / path).write_text(repaired)


def link_problems(extra: dict[str, str] | None = None) -> list[str]:
    return [problem for _, _, problem in link_failures(extra)]


def require_owned_outputs() -> None:
    problems = []
    for path in all_files():
        if not regular(path):
            problems.append(f"{path}: refusing nonregular output")
        elif path == refresh.PATH:
            try:
                refresh.read()
            except (ValueError, TypeError) as error:
                problems.append(f"{path}: refusing refresh without generator ownership: {error}")
        elif owner(path, (t.ROOT / path).read_text()) is None:
            problems.append(f"{path}: refusing output without generator ownership")
    if problems:
        raise SystemExit("\n".join(problems))


def present_problems() -> list[str]:
    problems = []
    inventory = frozenset(t.sources())
    model = t.os.environ.get("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    for path in all_files():
        if path == refresh.PATH:
            try:
                refresh.read()
            except (ValueError, TypeError) as error:
                problems.append(f"{path}: {error}")
            continue
        if not regular(path) or not path.endswith(".md"):
            problems.append(f"{path}: not a regular generated Markdown page")
            continue
        text = (t.ROOT / path).read_text()
        stamp = t.STAMP.match(text)
        current = bool(
            stamp and stamp[1] in inventory and stamp[2] == t.blob(stamp[1]) and stamp[3] == model
        )
        problems += candidate_problems(path, text, current=current)
    problems += link_problems()
    problems += t.untranslated_pages()
    problems += [
        f"{path}'s language line differs from the generated one"
        for path in t.language_lines_differ()
    ]
    return problems


def retire_obsolete(codes: list[str] | None = None) -> tuple[int, int]:
    """Remove only owned obsolete pages with no inbound dependency from retained output."""
    repair_english_fallbacks(codes)
    obsolete = t.orphans()
    candidates = {
        p
        for p in obsolete
        if regular(p)
        and owner(p, (t.ROOT / p).read_text())
        and (codes is None or p.split("/", 2)[1] in codes)
    }
    inbound: dict[str, set[str]] = {p: set() for p in candidates}
    for path in output_files():
        if not regular(path) or not path.endswith(".md"):
            continue
        text = (t.ROOT / path).read_text()
        for match in t.prose_links(text[body_start(path, text) :]):
            link = match[2].partition("#")[0]
            fallback = english_fallback(match[2])
            if fallback:
                target = t.target_path(path.split("/", 2)[1], fallback[0])
            elif re.match(r"[a-z]+:|/", link):
                continue
            else:
                target = (
                    posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(link)))
                    if link
                    else path
                )
            if target in inbound:
                inbound[target].add(path)
    while blocked := {p for p in candidates if inbound[p] - candidates}:
        candidates -= blocked
    for path in sorted(candidates):
        (t.ROOT / path).unlink()
    if candidates:
        # Retained obsolete pages stay served until retirement removes their native landing.
        t.write_language_lines(codes)
    return len(candidates), len(obsolete) - len(candidates)


def readme_publication_blocked(
    path: str, codes: list[str] | None, overlay: dict[str, str] | None = None
) -> bool:
    """A new README changes every published README's generated language links."""
    if regular(path) or path.split("/", 2)[-1] != "README.md":
        return False
    selected = {lang.code for lang in t.LANGUAGES} if codes is None else set(codes)
    job = refresh.plan()
    for other in t.readmes() + checkpoint_files():
        if not regular(other):
            continue
        text = (t.ROOT / other).read_text()
        owned = owner(other, text)
        if owned is None or owned[0] != "README.md":
            continue
        if owned[1].code not in selected or refresh.blocked(publication_path(other), job):
            return True
        if other in t.readmes() and candidate_problems(other, (overlay or {}).get(other, text)):
            return True
    return False


def current_readme_outputs(codes: list[str] | None) -> dict[str, str]:
    """Remember only current, verified output before deterministic link maintenance."""
    current = {}
    job = refresh.plan()
    for path in output_files() + checkpoint_files():
        if not regular(path):
            continue
        text = (t.ROOT / path).read_text()
        owned = owner(path, text)
        if (
            owned
            and owned[0] == "README.md"
            and (codes is None or owned[1].code in codes)
            and not refresh.blocked(publication_path(path), job)
            and not candidate_problems(path, text)
        ):
            current[path] = text
    return current


def rebase_readme_outputs(current: dict[str, str], english_before: str) -> None:
    """A generated language-bar edit does not require another model call."""
    if not current:
        return
    english_after = (t.ROOT / "README.md").read_text()
    if strip_language_line(english_before) != strip_language_line(english_after):
        raise ValueError("README prose changed during language-line maintenance")
    changed = {}
    for path, before in current.items():
        owned = owner(path, before)
        assert owned is not None
        _, lang = owned
        stamp = t.STAMP.match(before)
        assert stamp is not None
        header = t.header("README.md", lang, stamp[3], stamp[2])
        text = (t.ROOT / path).read_text()
        if not text.startswith(header):
            raise ValueError(f"{path}: README changed during language-line maintenance")
        line = t.LANGUAGE_LINE.search(text)
        if line is None:
            raise ValueError(f"{path}: README has no generated language markers")
        text = text[: line.start()] + t.language_line(publication_path(path)) + text[line.end() :]
        text = t.header("README.md", lang, stamp[3], t.blob("README.md")) + text[len(header) :]
        if candidate_problems(path, text):
            raise ValueError(f"{path}: README language-line repair failed validation")
        if text != before:
            (t.ROOT / path).write_text(text)
            changed[path] = (before, text)
    refresh.rebase_readme_proofs(changed)


def current_checkpoints() -> dict[str, str]:
    ready = {}
    job = refresh.plan()
    for path in checkpoint_files():
        if regular(path):
            text = (t.ROOT / path).read_text()
            published = publication_path(path)
            force = job and published.split("/", 2)[1] in job["languages"]
            if not candidate_problems(path, text) and (
                not force or refresh.proof_matches(published, text, job)
            ):
                ready[published] = text
    return ready


def stage(path: str, text: str) -> None:
    problems = candidate_problems(path, text)
    if problems:
        raise t.TranslationDiffers("; ".join(problems))
    destination = t.ROOT / checkpoint_path(publication_path(path))
    if any(parent.is_symlink() for parent in (destination, *destination.parents)):
        raise SystemExit(f"{path}: refusing symlink destination")
    if (
        destination.exists()
        and owner(destination.relative_to(t.ROOT).as_posix(), destination.read_text()) is None
    ):
        raise SystemExit(f"{path}: refusing checkpoint without generator ownership")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def activate_ready(codes: list[str] | None = None) -> int:
    """Install an overlay only when all present inbound and outbound links remain valid."""
    require_owned_outputs()
    ready = {
        path: text
        for path, text in current_checkpoints().items()
        if codes is None or path.split("/", 2)[1] in codes
    }
    while ready:
        blocked = {path for path in ready if readme_publication_blocked(path, codes, ready)}
        for path in sorted(blocked):
            print(f"deferred {path}: README language links require other locales")
        for source, target, _ in link_failures(ready):
            if target in ready and target != source:
                blocked.add(target)
            elif source in ready:
                blocked.add(source)
        if not blocked:
            break
        for path in blocked:
            del ready[path]
    for path, text in ready.items():
        destination = t.ROOT / path
        if any(parent.is_symlink() for parent in (destination, *destination.parents)):
            raise SystemExit(f"{path}: refusing symlink destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        (t.ROOT / checkpoint_path(path)).unlink()
    for path in checkpoint_files():
        if regular(path) and (codes is None or publication_path(path).split("/", 2)[1] in codes):
            text = (t.ROOT / path).read_text()
            if owner(path, text) and candidate_problems(path, text):
                (t.ROOT / path).unlink()
    t.write_language_lines(codes)
    refresh.finish()
    print(f"published={len(ready)} checkpointed={len(checkpoint_files())}")
    return len(ready)


def pending_blobs(ref: str) -> dict[str, str]:
    tree = subprocess.run(
        ["git", "ls-tree", "-rz", ref, "--", t.OUT_DIR, "README.md"],
        cwd=t.ROOT,
        check=True,
        capture_output=True,
    ).stdout
    blobs: dict[str, str] = {}
    pending_bytes = 0
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        meta, path_bytes = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        path = path_bytes.decode()
        if mode != "100644" or kind != "blob":
            raise SystemExit(f"{path}: pending output is not a regular blob")
        size = int(
            subprocess.run(
                ["git", "cat-file", "-s", oid], cwd=t.ROOT, check=True, capture_output=True
            ).stdout
        )
        if size > MAX_PAGE_BYTES:
            raise SystemExit(f"{path}: pending page exceeds size limit")
        pending_bytes += size
        if pending_bytes > MAX_PENDING_BYTES:
            raise SystemExit("pending translation data exceeds total size limit")
        data = subprocess.run(
            ["git", "cat-file", "blob", oid], cwd=t.ROOT, check=True, capture_output=True
        ).stdout
        blobs[path] = data.decode()
    return blobs


def preserve_deferred(path: str, text: str) -> bool:
    """Retain owned deferred data without granting it current-page validation or activation."""
    stamp = t.STAMP.match(text)
    problems = candidate_problems(path, text, current=False)
    if problems or stamp is None or stamp[3] not in t.PRICES:
        raise SystemExit(f"{path}: refusing deferred data without verified ownership/banner/model")
    published = publication_path(path)
    if regular(published) and (t.ROOT / published).read_bytes() == text.encode():
        return False
    destination = checkpoint_path(published)
    file = t.ROOT / destination
    if file.exists() or file.is_symlink():
        if regular(destination) and file.read_bytes() == text.encode():
            return False
        raise SystemExit(
            f"{destination}: cannot replace differing deferred data; pending replacement refused"
        )
    if any(parent.is_symlink() for parent in file.parents):
        raise SystemExit(f"{destination}: refusing symlink archive destination")
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(text.encode())
    return True


def rebase_pending_readmes(
    blobs: dict[str, str], english: str | None, codes: list[str] | None
) -> None:
    """Reuse verified data when main differs only in its generated language bar."""
    if english is None or not regular("README.md"):
        return
    if strip_language_line(english) != strip_language_line((t.ROOT / "README.md").read_text()):
        return
    old_blob = subprocess.run(
        ["git", "hash-object", "--stdin"],
        input=english,
        cwd=t.ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    current_blob = t.blob("README.md")
    if old_blob == current_blob:
        return
    for path, text in blobs.items():
        owned = owner(path, text)
        if (
            codes is not None
            and owned
            and owned[0] == "README.md"
            and owned[1].code not in codes
            and (stamp := t.STAMP.match(text))
            and text.startswith(t.header("README.md", owned[1], stamp[3], old_blob))
        ):
            raise SystemExit(
                "Pending README language links include excluded locales; "
                "rerun with the full approved language selection"
            )
    changed = {}
    for path, before in list(blobs.items()):
        owned, stamp = owner(path, before), t.STAMP.match(before)
        if not owned or owned[0] != "README.md" or not stamp:
            continue
        _, lang = owned
        header = t.header("README.md", lang, stamp[3], old_blob)
        if (codes is not None and lang.code not in codes) or not before.startswith(header):
            continue
        after = t.header("README.md", lang, stamp[3], current_blob) + before[len(header) :]
        line = t.LANGUAGE_LINE.search(after)
        if line is None:
            continue
        after = (
            after[: line.start()] + t.language_line(publication_path(path)) + after[line.end() :]
        )
        if not candidate_problems(path, after):
            blobs[path] = after
            changed[path] = (before, after)
    if refresh.PATH in blobs:
        blobs[refresh.PATH] = refresh.rebase_imported_readme_proofs(blobs[refresh.PATH], changed)


def import_pending(ref: str, codes: list[str] | None = None) -> int:
    """Read only blob objects, never branch code; current main output wins."""
    require_owned_outputs()
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise SystemExit("pending ref must be an exact commit SHA")
    blobs = pending_blobs(ref)
    english = blobs.pop("README.md", None)
    for path, text in blobs.items():
        if path != refresh.PATH and owner(path, text) is None:
            raise SystemExit(f"{path}: refusing unknown pending output")
    rebase_pending_readmes(blobs, english, codes)
    refresh.clear_merged()
    force = refresh.import_receipt(blobs[refresh.PATH], blobs) if refresh.PATH in blobs else set()
    proposed: dict[str, str] = {}
    preserved = 0
    model = t.os.environ.get("LOOM_I18N_MODEL", t.DEFAULT_MODEL)
    job = refresh.plan()
    for path, text in blobs.items():
        if path == refresh.PATH:
            continue
        owned = owner(path, text)
        if owned is None:
            raise SystemExit(f"{path}: refusing unknown pending output")
        source, lang = owned
        if codes is not None and lang.code not in codes:
            preserved += preserve_deferred(path, text)
            continue
        path = publication_path(path)
        if source not in t.sources():
            continue
        if path in force and job and not refresh.proof_matches(path, text, job, replacement=False):
            continue
        if (
            path not in force
            and regular(path)
            and (t.ROOT / path)
            .read_text()
            .startswith(t.header(source, lang, model, t.blob(source)))
        ):
            continue
        problems = candidate_problems(path, text)
        if problems:
            print("pending output rejected: " + "; ".join(problems))
        else:
            proposed[path] = text
    for path, text in proposed.items():
        stage(path, text)
    activate_ready(codes)
    print(f"reused={len(proposed)} preserved_deferred={preserved}")
    return len(proposed) + preserved
