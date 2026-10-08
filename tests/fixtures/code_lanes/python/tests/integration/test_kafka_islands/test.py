"""Synthetic ClickHouse integration test: SQL through the harness client."""

import pytest
from helpers.cluster import ClickHouseCluster

cluster = ClickHouseCluster(__file__)
node = cluster.add_instance("node", with_kafka=True)


@pytest.fixture(scope="module")
def started_cluster():
    try:
        cluster.start()
        yield cluster
    finally:
        cluster.shutdown()


def test_kafka_engine(started_cluster):
    node.query(
        """
        CREATE TABLE kafka_src (key UInt64, value String)
        ENGINE = Kafka
        SETTINGS kafka_broker_list = 'kafka1:19092', kafka_topic_list = 'topic',
                 kafka_group_name = 'g', kafka_format = 'JSONEachRow';
        """
    )
    node.query("CREATE TABLE kafka_dst (key UInt64, value String) ENGINE = MergeTree ORDER BY key")
    node.query(
        "CREATE MATERIALIZED VIEW kafka_mv TO kafka_dst AS SELECT key, value FROM kafka_src"
    )
    for i in range(3):
        node.query(f"INSERT INTO kafka_dst VALUES ({i}, 'v')")
    result = node.query("SELECT count() FROM kafka_dst FINAL SETTINGS max_threads = 1")
    assert result.strip() == "3"
    error = node.query_and_get_error("DROP TABLE nonexistent_table")
    assert "UNKNOWN_TABLE" in error
    node.query("SELECT * FROM system.parts WHERE table = '{}'".format("kafka_dst"))
    node.query("DROP TABLE kafka_mv")
    node.query("DROP TABLE kafka_src")


def test_not_sql(started_cluster):
    # docstring-like and prose strings are not islands
    message = "Select the broker before creating the table"
    assert node.query("SELECT 1") == "1\n"
    assert message
