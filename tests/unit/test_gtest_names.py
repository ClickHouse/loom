"""A googletest body is named after its suite and test."""

from __future__ import annotations

from server.code.domain.extract.treesitter import parse_cpp
from server.code.domain.index.test_gate import is_test_path

SRC = b"""#include <gtest/gtest.h>
namespace lumen {

static int helper(int x) { return x + 1; }

TEST(Crc32cTest, MatchesStandardCheckVector) {
    EXPECT_EQ(helper(1), 2);
}

TEST_F(LogStoreFixture, AppendsInOrder) {
    ASSERT_TRUE(helper(0));
}

TYPED_TEST(PageStoreTyped, RoundTrips) {}

void notATest(int a, int b) {}

}  // namespace lumen
"""


def test_gtest_bodies_are_named_suite_and_test() -> None:
    pf = parse_cpp("src/util/tests/crc-test.cpp", SRC)
    by = {s.qualified_name: s for s in pf.symbols}
    assert "lumen::Crc32cTest::MatchesStandardCheckVector" in by
    assert "lumen::LogStoreFixture::AppendsInOrder" in by
    assert "lumen::PageStoreTyped::RoundTrips" in by
    assert "lumen::TEST" not in by and "lumen::TEST_F" not in by
    t = by["lumen::Crc32cTest::MatchesStandardCheckVector"]
    assert (
        t.kind == "test" and t.name == "MatchesStandardCheckVector"
    )  # A test case, not a callable
    assert t.signature == "TEST(Crc32cTest, MatchesStandardCheckVector)"
    assert (t.start_line, t.end_line) == (6, 8)
    # ordinary functions keep their parameter-list signature
    assert (
        by["lumen::notATest"].signature == "(int a, int b)"
        and by["lumen::helper"].kind == "function"
    )
    assert len({s.id for s in pf.symbols}) == len(pf.symbols), "no two tests collide on one id"


def test_hyphenated_test_files_count_as_tests() -> None:
    assert is_test_path("src/util/tests/crc-test.cpp")
    # a name that merely contains -test- is decided by its directory
    assert not is_test_path("src/storage/channel-test-faults.cpp")
    assert is_test_path("src/storage/tests/channel-test-faults.cpp")
    assert is_test_path("lib/foo-test.cc") and not is_test_path("lib/foo-tester.cpp")
