file(GLOB SOURCES CONFIGURE_DEPENDS *.cpp)

# The ledger library: log-store and page-store.
add_library(ledger-lib ${SOURCES})
target_link_libraries(ledger-lib PUBLIC ledger-util PRIVATE Boost::headers)

add_library(ledger-util util.cpp)
