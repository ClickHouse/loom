"""On a bare name, the hand-written definition ranks above
the generated file's declaration of the same name (a `.proto` message above
its `.pb.go` struct, the source above `zz_generated`, the type above the
mock), and a type-shaped query answers types before fields and methods.
Without this, with generated files declared, proto-message questions answer
the `.pb.go` struct first."""

from __future__ import annotations

import pytest

from server.code.store.index import (
    demote_generated_paths,
    demote_noise_paths,
    is_generated_path,
)


@pytest.mark.parametrize(
    "path, generated",
    [
        ("api/achievements/v1/achievements.pb.go", True),
        ("api/achievements/v1/achievements_grpc.pb.go", True),
        ("api/foo/v1/foo.pb.gw.go", True),
        ("api/foo/v1/foo.pb.validate.go", True),
        ("api/foo/v1/foov1connect/foo.connect.go", True),
        ("apis/v1/zz_generated.deepcopy.go", True),
        ("gen/proto/foo_pb2.py", True),
        ("gen/proto/foo_pb2_grpc.py", True),
        ("gen/proto/foo_pb2.pyi", True),
        ("web/src/gen/foo.pb.ts", True),
        ("web/src/gen/foo_pb.js", True),
        ("web/src/gen/foo_pb_service.d.ts", True),
        ("ent/user_gen.go", True),
        ("db/queries.gen.go", True),
        ("graph/generated.go", False),  # a bare `generated.go` is a name, not a marker
        ("graph/generated/generated.go", True),
        ("graph/schema.generated.go", True),
        ("internal/mocks/store.go", True),
        ("internal/store/mock_store.go", True),
        ("internal/store/store_mock.go", True),
        ("lib/model.g.dart", True),
        ("api/achievements/v1/achievements.proto", False),
        ("internal/store/store.go", False),
        ("internal/mockingbird/feeder.go", False),
        ("cmd/pbserver/main.go", False),
        ("", False),
    ],
)
def test_generated_path_shapes(path: str, generated: bool) -> None:
    assert is_generated_path(path) is generated


def test_hand_written_first_then_generated_then_noise() -> None:
    rows = [
        {"path": "api/v1/a.pb.go", "qualified_name": "api.v1.apiv1::Req", "kind": "struct"},
        {"path": "api/v1/a.proto", "qualified_name": "api.v1.a::Req", "kind": "struct"},
        {"path": "api/v1/a_test.go", "qualified_name": "api.v1.apiv1::Req", "kind": "struct"},
        {
            "path": "internal/mocks/a.go",
            "qualified_name": "internal.mocks.mocks::Req",
            "kind": "struct",
        },
    ]
    got = [r["path"] for r in demote_generated_paths(demote_noise_paths(rows))]
    assert got == ["api/v1/a.proto", "api/v1/a.pb.go", "internal/mocks/a.go", "api/v1/a_test.go"]


def test_partition_is_stable_within_groups() -> None:
    rows = [{"path": f"gen/{i}.pb.go"} for i in range(3)] + [
        {"path": f"src/{i}.go"} for i in range(3)
    ]
    got = [r["path"] for r in demote_generated_paths(rows)]
    assert got == ["src/0.go", "src/1.go", "src/2.go", "gen/0.pb.go", "gen/1.pb.go", "gen/2.pb.go"]
