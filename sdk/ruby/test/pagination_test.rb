# frozen_string_literal: true

require_relative "test_helper"

class PaginationTest < LoomTest
  # A page source that answers from +pages+ in order and records the cursors it was asked for.
  def pages_from(*pages)
    asked = []
    fetch = lambda do |cursor|
      asked << cursor
      pages.fetch(asked.size - 1)
    end
    [fetch, asked]
  end

  def test_walks_the_pages_until_there_is_no_next_cursor
    fetch, asked = pages_from({ "rows" => [1, 2], "next_cursor" => "c2" },
                              { "rows" => [3], "next_cursor" => "c3" },
                              { "rows" => [4], "next_cursor" => nil })

    assert_equal [1, 2, 3, 4], Loom::Pagination.paginate(&fetch).to_a
    assert_equal [nil, "c2", "c3"], asked
  end

  def test_reads_the_results_shape_and_a_page_without_rows
    fetch, = pages_from({ "results" => %w[a b], "next_cursor" => "c2" }, { "next_cursor" => "" })

    assert_equal %w[a b], Loom::Pagination.paginate(&fetch).to_a
  end

  def test_stops_when_a_page_repeats_the_cursor_it_was_fetched_with
    fetch, asked = pages_from({ "rows" => [1], "next_cursor" => "same" },
                              { "rows" => [2], "next_cursor" => "same" })

    assert_equal [1, 2], Loom::Pagination.paginate(&fetch).to_a
    assert_equal [nil, "same"], asked
  end

  def test_stops_after_max_pages
    fetch, asked = pages_from({ "rows" => [1], "next_cursor" => "c2" },
                              { "rows" => [2], "next_cursor" => "c3" },
                              { "rows" => [3], "next_cursor" => "c4" })

    assert_equal [1, 2], Loom::Pagination.paginate(max_pages: 2, &fetch).to_a
    assert_equal 2, asked.size
    assert_empty Loom::Pagination.paginate(max_pages: 0, &fetch).to_a
  end

  def test_fetches_a_page_only_when_its_rows_are_needed
    fetch, asked = pages_from({ "rows" => [1, 2], "next_cursor" => "c2" },
                              { "rows" => [3], "next_cursor" => nil })

    assert_equal [1, 2], Loom::Pagination.paginate(&fetch).first(2)
    assert_equal [nil], asked
  end

  def test_accepts_a_page_with_symbol_keys
    fetch, = pages_from({ rows: [1], next_cursor: "c2" }, { rows: [2] })

    assert_equal [1, 2], Loom::Pagination.paginate(&fetch).to_a
  end

  def test_rejects_a_missing_block_and_a_max_pages_that_is_not_an_integer
    assert_raises(ArgumentError) { Loom::Pagination.paginate }
    assert_raises(ArgumentError) { Loom::Pagination.paginate(max_pages: "2") { {} } }
  end

  def test_client_paginate_sends_each_cursor_to_the_op
    respond_json(200, { rows: [{ "uri" => "a" }], next_cursor: "c2" })
    respond_json(200, { rows: [{ "uri" => "b" }], next_cursor: nil })
    api = client

    rows = api.paginate { |cursor| api.knowledge.list(limit: 1, cursor: cursor) }.to_a

    assert_equal [{ "uri" => "a" }, { "uri" => "b" }], rows
    bodies = @server.requests.map { |request| JSON.parse(request.body) }
    assert_equal %w[/v1/knowledge.list /v1/knowledge.list], @server.requests.map(&:path)
    refute bodies[0].key?("cursor")
    assert_equal "c2", bodies[1]["cursor"]
    assert_equal 1, bodies[1]["limit"]
  end
end
