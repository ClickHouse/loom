add_executable(ledger-test ledger-test.cpp)
target_link_libraries(ledger-test PRIVATE ledger-lib GTest::gtest_main)
gtest_discover_tests(ledger-test)
