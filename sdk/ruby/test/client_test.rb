# frozen_string_literal: true

require_relative "test_helper"

class ClientTest < LoomTest
  def test_set_posts_the_memory_to_memory_set_and_returns_the_row
    respond_json(200, { memory_id: "m1", version: 1_763_000_000_000_000_001, content_sha256: "abc",
                        status: "active", noop: false })

    out = client(api_key: "tok").set("deploy_window", "Deploys run Tue and Thu only.",
                                     memory_type: "preference", tags: %w[ops])

    assert_equal "POST", sent.method
    assert_equal "/v1/memory.set", sent.path
    assert_equal "application/json", sent.headers["content-type"]
    assert_equal "Bearer tok", sent.headers["authorization"]
    assert_equal "loom-ruby/#{Loom::VERSION}", sent.headers["x-loom-sdk"]
    assert_equal "loom-ruby/#{Loom::VERSION}", sent.headers["user-agent"]
    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant",
                   "memory_key" => "deploy_window", "value" => "Deploys run Tue and Thu only.",
                   "memory_type" => "preference", "tags" => %w[ops] }, sent_body)
    assert_equal Loom::SetResult.new(memory_id: "m1", version: 1_763_000_000_000_000_001,
                                     content_sha256: "abc", status: "active", noop: false), out
  end

  def test_set_leaves_out_the_options_it_was_not_given_and_sends_a_structured_value_as_is
    respond_json(200, { memory_id: "m1", version: 1, noop: true })

    out = client.set("config", { "retries" => 3, "hosts" => %w[a b] })

    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant", "memory_key" => "config",
                   "value" => { "retries" => 3, "hosts" => %w[a b] } }, sent_body)
    assert out.noop
  end

  def test_get_posts_the_key_and_returns_the_stored_value
    respond_json(200, { found: true, memory_id: "m1", value: "Deploys run Tue and Thu only.",
                        memory_type: "semantic", version: 7, tags: %w[ops] })

    out = client.get("deploy_window")

    assert_equal "/v1/memory.get", sent.path
    assert_equal "deploy_window", sent_body["memory_key"]
    assert out.found
    assert_equal "Deploys run Tue and Thu only.", out.value
    assert_equal %w[ops], out.tags
    assert_equal 7, out.version
  end

  def test_get_of_a_missing_key_is_not_an_error
    respond_json(200, { found: false })

    out = client.get("nope")

    refute out.found
    assert_nil out.value
    assert_empty out.tags
  end

  def test_search_posts_the_query_and_maps_the_hits_in_order
    respond_json(200, { results: [
                    { memory_key: "a", memory_type: "semantic", content_excerpt: "first", similarity: 0.91, version: 1 },
                    { memory_key: "b", memory_type: "semantic", content_excerpt: "second", similarity: 0.5, version: 2 }
                  ] })

    out = client.search("deploy window", top_k: 2, memory_type: "semantic", tags: %w[ops])

    assert_equal "/v1/memory.search", sent.path
    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant", "query" => "deploy window",
                   "top_k" => 2, "memory_type" => "semantic", "tags" => %w[ops] }, sent_body)
    assert_equal %w[a b], out.results.map(&:memory_key)
    assert_equal "first", out.results.first.content_excerpt
    assert_in_delta 0.91, out.results.first.similarity
  end

  def test_search_with_no_hits_returns_an_empty_list
    respond_json(200, { results: [], abstain: true })

    assert_empty client.search("nothing like this").results
  end

  def test_delete_posts_the_key_and_reports_the_tombstone
    respond_json(200, { found: true, tombstoned: true, version: 9 })

    out = client.delete("deploy_window")

    assert_equal "/v1/memory.delete", sent.path
    assert_equal "deploy_window", sent_body["memory_key"]
    assert_equal Loom::DeleteResult.new(found: true, tombstoned: true, version: 9), out
  end

  def test_a_blank_identity_is_left_out_of_the_body_and_no_token_sends_no_authorization
    respond_json(200, { found: false })

    Loom::Client.new(base_url: @server.base_url, org: "dev", namespace: " ", agent: nil).get("k")

    assert_equal({ "org" => "dev", "memory_key" => "k" }, sent_body)
    refute sent.headers.key?("authorization")
  end

  def test_the_environment_supplies_the_server_and_the_token
    respond_json(200, { found: false })
    ENV["LOOM_BASE_URL"] = "#{@server.base_url}/"
    ENV["LOOM_API_KEY"] = "env-token"

    Loom::Client.new.get("k")

    assert_equal "/v1/memory.get", sent.path
    assert_equal "Bearer env-token", sent.headers["authorization"]
  end

  def test_an_explicit_argument_wins_over_the_environment
    respond_json(200, { found: false })
    ENV["LOOM_BASE_URL"] = "http://127.0.0.1:1"
    ENV["LOOM_API_KEY"] = "env-token"

    client(api_key: "arg-token").get("k")

    assert_equal "Bearer arg-token", sent.headers["authorization"]
  end

  def test_a_blank_token_in_the_environment_sends_no_authorization
    respond_json(200, { found: false })
    ENV["LOOM_API_KEY"] = "  "

    client.get("k")

    refute sent.headers.key?("authorization")
  end
end
