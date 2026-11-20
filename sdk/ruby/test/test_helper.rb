# frozen_string_literal: true

require "json"
require "minitest/autorun"

require "clickhouse/loom"
require_relative "fake_server"

class LoomTest < Minitest::Test
  Loom = ClickHouse::Loom

  def setup
    @server = FakeServer.new
    @saved_env = ENV.to_h.slice("LOOM_BASE_URL", "LOOM_API_KEY")
    ENV.delete("LOOM_BASE_URL")
    ENV.delete("LOOM_API_KEY")
  end

  def teardown
    @server.close
    ENV.delete("LOOM_BASE_URL")
    ENV.delete("LOOM_API_KEY")
    @saved_env.each { |name, value| ENV[name] = value }
  end

  def client(**options)
    Loom::Client.new(base_url: @server.base_url, org: "dev", namespace: "local", agent: "assistant", **options)
  end

  def respond_json(status, body, headers = {})
    @server.respond(status, JSON.generate(body), headers)
  end

  def sent
    @server.requests.last
  end

  def sent_body
    JSON.parse(sent.body)
  end
end
