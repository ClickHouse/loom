# frozen_string_literal: true

require_relative "test_helper"

class ConfigurationTest < LoomTest
  def test_configure_sets_the_defaults_of_a_client_built_without_arguments
    respond_json(200, { found: false })
    Loom.configure do |c|
      c.base_url = @server.base_url
      c.api_key = "configured-token"
      c.org = "acme"
      c.namespace = "support"
      c.agent = "triage"
    end

    Loom::Client.new.memory.get(memory_key: "k")

    assert_equal "Bearer configured-token", sent.headers["authorization"]
    assert_equal({ "org" => "acme", "namespace" => "support", "agent" => "triage", "memory_key" => "k" }, sent_body)
  end

  def test_an_argument_wins_over_configure_and_configure_over_the_environment
    respond_json(200, { found: false })
    ENV["LOOM_BASE_URL"] = "http://127.0.0.1:1"
    ENV["LOOM_API_KEY"] = "env-token"
    Loom.configure do |c|
      c.base_url = @server.base_url
      c.api_key = "configured-token"
      c.namespace = "support"
    end

    c = Loom::Client.new(api_key: "arg-token", namespace: "user-42")
    c.memory.get(memory_key: "k")

    assert_equal @server.base_url, c.base_url
    assert_equal "Bearer arg-token", sent.headers["authorization"]
    assert_equal "user-42", sent_body["namespace"]
  end

  def test_configure_sets_the_retry_policy_and_a_client_argument_refines_it
    Loom.configure { |c| c.retry_policy = { max_attempts: 5, backoff: 0.1 } }

    policy = Loom::Client.new(retry_policy: { backoff: 0.2 }).retry_policy

    assert_equal 5, policy.max_attempts
    assert_in_delta 0.2, policy.backoff
  end

  def test_a_bad_configured_policy_names_where_it_came_from
    Loom.configure { |c| c.retry_policy = { retry_timeouts: :sometimes } }

    error = assert_raises(ArgumentError) { Loom::Client.new }

    assert_includes error.message, "ClickHouse::Loom.configure"
  end

  def test_the_short_name_is_the_gem_module
    assert_same ClickHouse::Loom, ::Loom
  end

  def test_inspect_names_the_server_and_identity_but_never_the_token
    text = client(api_key: "secret-token").inspect

    assert_includes text, "org=dev"
    refute_includes text, "secret-token"
  end
end
