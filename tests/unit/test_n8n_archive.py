import base64
import copy
import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.gates import n8n_archive as checker


class ArchiveChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = {
            "name": "@clickhouse/loom",
            "version": "0.0.0-synthetic-only",
            "type": "module",
            "n8n": {
                "n8nNodesApiVersion": 1,
                "credentials": [checker.NATIVE + "credentials/LoomApi.credentials.js"],
                "nodes": [
                    checker.NATIVE + "nodes/Loom/Loom.node.js",
                    checker.NATIVE + "nodes/LoomMemory/LoomMemory.node.js",
                ],
            },
            "exports": {
                "./integrations/n8n": {
                    "import": {
                        "types": "./dist/integrations/n8n.d.mts",
                        "default": "./dist/integrations/n8n.mjs",
                    },
                    "require": {
                        "types": "./dist/integrations/n8n.d.cts",
                        "default": "./dist/integrations/n8n.cjs",
                    },
                }
            },
            "peerDependencies": {"n8n-workflow": ">=2.47.2 <3"},
            "peerDependenciesMeta": {"n8n-workflow": {"optional": True}},
        }
        self.files = {name: b"synthetic inert bytes" for name in checker.REQUIRED}
        self.files[checker.NATIVE + "package.json"] = b'{"type":"commonjs"}'
        for suffix in (".mjs", ".cjs", ".d.mts", ".d.cts"):
            self.files["dist/integrations/n8n" + suffix] = b"throw new Error('MUST NOT EXECUTE');"

    def build(self, manifest=None, extra=(), raw_manifest=None, extra_first=False):
        actual = self.manifest if manifest is None else manifest
        source = json.dumps(self.manifest).encode()
        source_path = self.root / "source.json"
        source_path.write_bytes(source)
        files = dict(self.files)
        files["package.json"] = (
            json.dumps(actual).encode() if raw_manifest is None else raw_manifest
        )
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:") as archive:
            if extra_first:
                for info, value in extra:
                    archive.addfile(info, io.BytesIO(value) if value else None)
            for name, value in files.items():
                info = tarfile.TarInfo("package/" + name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            if not extra_first:
                for info, value in extra:
                    archive.addfile(info, io.BytesIO(value) if value else None)
        packed = gzip.compress(data.getvalue(), mtime=0)
        path = self.root / "synthetic.tgz"
        path.write_bytes(packed)
        return [
            path,
            source_path,
            hashlib.sha256(source).hexdigest(),
            self.manifest["version"],
            "sha512-" + base64.b64encode(hashlib.sha512(packed).digest()).decode(),
        ]

    def test_valid_static_inspection_leaves_only_input_files(self):
        args = self.build()
        before = sorted(p.name for p in self.root.iterdir())
        result = checker.inspect_archive(*args)
        self.assertEqual(result["archive_inspection"], "PASS")
        self.assertEqual(result["runtime"], "UNRUN")
        self.assertEqual(
            result["publication_source_mapping_and_provenance"], "NOT_VERIFIED_BY_THIS_CHECK"
        )
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_integrity_and_source_digest_are_required(self):
        for index in (2, 4):
            with self.subTest(index=index):
                args = self.build()
                args[index] = "wrong"
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*args)

    def test_each_native_member_and_export_is_required(self):
        for name in list(self.files):
            with self.subTest(name=name):
                saved = self.files.pop(name)
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    self.files[name] = saved

    def test_empty_licenses_refused(self):
        for name in (checker.NATIVE + "LICENSE", checker.NATIVE + "THIRD_PARTY_LICENSES.txt"):
            with self.subTest(name=name):
                saved = self.files[name]
                self.files[name] = b" \n"
                try:
                    with self.assertRaises(ValueError) as error:
                        checker.inspect_archive(*self.build())
                    self.assertEqual(
                        str(error.exception), "missing or empty required member: " + name
                    )
                finally:
                    self.files[name] = saved

    def test_missing_commonjs_boundary_refused(self):
        self.files[checker.NATIVE + "package.json"] = b'{"type":"module"}'
        with self.assertRaisesRegex(ValueError, "CommonJS"):
            checker.inspect_archive(*self.build())

    def test_changed_manifest_contracts_refused(self):
        for field, value in (
            ("version", "wrong"),
            ("name", "other"),
            ("type", "commonjs"),
            ("n8n", {}),
            ("peerDependencies", {}),
            ("peerDependenciesMeta", {}),
            ("exports", {}),
        ):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.manifest)
                actual[field] = value
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(actual))

    def test_runtime_dependency_refused(self):
        for field in (
            "dependencies",
            "optionalDependencies",
            "bundledDependencies",
            "bundleDependencies",
        ):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.manifest)
                actual[field] = (
                    {"n8n-workflow": "2.48.3"}
                    if field.endswith("ependencies")
                    and field not in ("bundledDependencies", "bundleDependencies")
                    else ["n8n-workflow"]
                )
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(actual))

    def test_duplicate_json_keys_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            checker.inspect_archive(*self.build(raw_manifest=b'{"name":"a","name":"b"}'))

    def test_path_aliases_duplicates_and_traversal_refused(self):
        for name in (
            "../outside",
            "/package/outside",
            "package/../outside",
            "package//alias",
            "package/./alias",
            "package/dir\\alias",
            "package/package.json",
        ):
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                info.size = 1
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(extra=[(info, b"x")]))

    def test_links_and_special_members_refused(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("package/link")
                info.type = kind
                info.linkname = "package/package.json"
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(extra=[(info, b"")]))

    def test_long_and_deep_paths_refused_before_parent_materialization(self):
        for name in (
            "package/" + "x" * 5_000,
            "package/" + "x/" * 128 + "file",
            "package/" + "é" * 2_100,
        ):
            with self.subTest(name_length=len(name)):
                info = tarfile.TarInfo(name)
                info.size = 1
                with self.assertRaisesRegex(ValueError, "archive path exceeds inspection bound"):
                    checker.inspect_archive(*self.build(extra=[(info, b"x")]))

    def test_compressed_input_bound_enforced_before_decompression(self):
        args = self.build()
        original = checker.MAX_COMPRESSED
        checker.MAX_COMPRESSED = 1
        try:
            with self.assertRaisesRegex(ValueError, "input exceeds"):
                checker.inspect_archive(*args)
        finally:
            checker.MAX_COMPRESSED = original

    def test_decompressed_and_count_bounds_enforced(self):
        args = self.build()
        for field, reason in (
            ("MAX_TAR", "decompressed archive exceeds"),
            ("MAX_MEMBERS", "too many archive members"),
        ):
            with self.subTest(field=field):
                original = getattr(checker, field)
                setattr(checker, field, 1)
                try:
                    with self.assertRaisesRegex(ValueError, reason):
                        checker.inspect_archive(*args)
                finally:
                    setattr(checker, field, original)

    def test_member_bound_after_valid_source_read(self):
        info = tarfile.TarInfo("package/oversized")
        info.size = 4097
        args = self.build(extra=[(info, b"x" * 4097)])
        self.assertLess(args[1].stat().st_size, 4096)
        original = checker.MAX_MEMBER
        checker.MAX_MEMBER = 4096
        try:
            with self.assertRaisesRegex(ValueError, "archive member exceeds inspection bound"):
                checker.inspect_archive(*args)
        finally:
            checker.MAX_MEMBER = original

    def test_non_json_constants_in_root_and_native_manifest_refused(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant, where="root"):
                raw = json.dumps(self.manifest).encode()[:-1] + b',"extra":' + constant + b"}"
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(raw_manifest=raw))
            with self.subTest(constant=constant, where="native"):
                self.files[checker.NATIVE + "package.json"] = (
                    b'{"type":"commonjs","extra":' + constant + b"}"
                )
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    self.files[checker.NATIVE + "package.json"] = b'{"type":"commonjs"}'

    def test_boolean_api_version_refused(self):
        actual = copy.deepcopy(self.manifest)
        actual["n8n"]["n8nNodesApiVersion"] = True
        with self.assertRaises(ValueError):
            checker.inspect_archive(*self.build(actual))

    def test_non_utf8_source_root_and_native_json_refused(self):
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding, where="root"):
                with self.assertRaises(ValueError):
                    checker.inspect_archive(
                        *self.build(raw_manifest=json.dumps(self.manifest).encode(encoding))
                    )
            with self.subTest(encoding=encoding, where="native"):
                self.files[checker.NATIVE + "package.json"] = '{"type":"commonjs"}'.encode(encoding)
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    self.files[checker.NATIVE + "package.json"] = b'{"type":"commonjs"}'
            with self.subTest(encoding=encoding, where="source"):
                args = self.build()
                source = json.dumps(self.manifest).encode(encoding)
                args[1].write_bytes(source)
                args[2] = hashlib.sha256(source).hexdigest()
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*args)

    def test_descendant_package_boundaries_refused(self):
        for data in (b'{"type":"module"}', b"not JSON"):
            with self.subTest(data=data):
                name = checker.NATIVE + "nodes/package.json"
                self.files[name] = data
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    del self.files[name]

    def test_invalid_condition_mapping_in_both_source_and_archive_refused(self):
        original = copy.deepcopy(self.manifest["exports"]["./integrations/n8n"])
        swapped = copy.deepcopy(original)
        swapped["import"], swapped["require"] = swapped["require"], swapped["import"]
        for mapping in ({"blocked": original}, swapped):
            with self.subTest(mapping=mapping):
                self.manifest["exports"]["./integrations/n8n"] = mapping
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    self.manifest["exports"]["./integrations/n8n"] = original

    def test_file_ancestor_conflicts_refused_in_both_orders(self):
        for first in (False, True):
            with self.subTest(first=first):
                info = tarfile.TarInfo("package/dist")
                info.size = 1
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.build(extra=[(info, b"x")], extra_first=first))

    def rewrite_tar(self, args, unpacked):
        packed = gzip.compress(unpacked, mtime=0)
        args[0].write_bytes(packed)
        args[4] = "sha512-" + base64.b64encode(hashlib.sha512(packed).digest()).decode()
        return args

    def test_nonzero_tail_and_concatenated_tar_refused(self):
        for suffix in (b"uninspected tail", b"second archive"):
            with self.subTest(suffix=suffix):
                args = self.build()
                data = gzip.decompress(args[0].read_bytes())
                if suffix == b"second archive":
                    suffix = data
                with self.assertRaises(ValueError):
                    checker.inspect_archive(*self.rewrite_tar(args, data + suffix))

    def test_missing_end_markers_refused(self):
        args = self.build()
        data = gzip.decompress(args[0].read_bytes())
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            end = max(member.offset_data + ((member.size + 511) // 512) * 512 for member in archive)
        with self.assertRaises(ValueError):
            checker.inspect_archive(*self.rewrite_tar(args, data[:end]))

    def test_core_package_without_native_registration_is_refused(self):
        del self.manifest["n8n"]
        with self.assertRaisesRegex(ValueError, "n8n must be an object"):
            checker.inspect_archive(*self.build())

    def test_matching_malformed_source_and_archive_fields_are_refused(self):
        original = copy.deepcopy(self.manifest)
        for field in ("n8n", "peerDependencies", "peerDependenciesMeta", "exports"):
            with self.subTest(field=field):
                self.manifest[field] = []
                try:
                    with self.assertRaises(ValueError):
                        checker.inspect_archive(*self.build())
                finally:
                    self.manifest = copy.deepcopy(original)
        self.manifest["peerDependenciesMeta"]["n8n-workflow"] = []
        with self.assertRaises(ValueError):
            checker.inspect_archive(*self.build())

    def invoke_cli(self, args):
        archive, source, source_sha, version, integrity = args
        output = io.StringIO()
        with redirect_stdout(output):
            code = checker.main(
                [
                    str(archive),
                    "--source-manifest",
                    str(source),
                    "--source-sha256",
                    source_sha,
                    "--version",
                    version,
                    "--integrity",
                    integrity,
                ]
            )
        return code, json.loads(output.getvalue())

    def test_cli_checks_candidate_without_claiming_runtime_or_publication(self):
        args = self.build()
        code, result = self.invoke_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(result["archive_inspection"], "PASS")
        self.assertEqual(result["runtime"], "UNRUN")
        self.assertEqual(
            result["publication_source_mapping_and_provenance"], "NOT_VERIFIED_BY_THIS_CHECK"
        )
        self.assertEqual(result["marketplace_acceptance"], "NOT_ESTABLISHED")
        args[4] = "wrong"
        code, result = self.invoke_cli(args)
        self.assertEqual(code, 1)
        self.assertEqual(result["archive_inspection"], "FAIL")
        self.assertEqual(result["reason"], "archive integrity mismatch")

    def test_corrupt_compressed_input_reports_failure_without_traceback(self):
        for corrupt in (
            b"not a gzip archive: synthetic secret",
            b"\x1f\x8b\x08\x00" + b"\x00" * 6 + b"\x07" + b"\x00" * 8,
        ):
            with self.subTest(corrupt=corrupt):
                args = self.build()
                args[0].write_bytes(corrupt)
                args[4] = "sha512-" + base64.b64encode(hashlib.sha512(corrupt).digest()).decode()
                code, result = self.invoke_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(result["archive_inspection"], "FAIL")
                self.assertEqual(result["runtime"], "UNRUN")
                self.assertNotIn("synthetic secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
