# frozen_string_literal: true

require "json"
require "minitest/autorun"
require "fileutils"
require "tmpdir"

require "clickhouse/loom"
require_relative "fake_server"

class LoomTest < Minitest::Test
  Loom = ClickHouse::Loom

  def setup
    @server = FakeServer.new
    @saved_env = ENV.to_h.slice("LOOM_BASE_URL", "LOOM_API_KEY", "HOME")
    # A home of its own, so a developer's ~/.loom/token never reaches a test.
    @home = Dir.mktmpdir("loom-home")
    ENV["HOME"] = @home
    ENV.delete("LOOM_BASE_URL")
    ENV.delete("LOOM_API_KEY")
    Loom.reset_configuration!
  end

  def teardown
    @server.close
    ENV.delete("LOOM_BASE_URL")
    ENV.delete("LOOM_API_KEY")
    ENV.delete("HOME")
    @saved_env.each { |name, value| ENV[name] = value }
    FileUtils.rm_rf(@home)
    Loom.reset_configuration!
  end

  # One attempt and no wait on a 429, so a test reads the first answer as it came; the
  # retry tests pass their own policy.
  def client(**options)
    defaults = { base_url: @server.base_url, org: "dev", namespace: "local", agent: "assistant",
                 retry_policy: { max_attempts: 1 }, rate_limit_max_wait: 0 }
    Loom::Client.new(**defaults, **options)
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
