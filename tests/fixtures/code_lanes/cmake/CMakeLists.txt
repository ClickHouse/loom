cmake_minimum_required(VERSION 3.28)
project(ledger CXX)

# Build the benchmarks too.
option(BUILD_BENCHMARKS "Build the benchmark binaries" ON)

add_subdirectory(lib)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
