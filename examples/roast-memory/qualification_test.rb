# frozen_string_literal: true

require "minitest/autorun"
require "socket"
require "open3"
require "rbconfig"
require "timeout"
require_relative "memory_step"

# Loopback contract fixture, not a Loom server or an authorization implementation.
class MemoryFixture
  attr_reader :requests, :port
  attr_accessor :response

  def initialize
    @server = TCPServer.new("127.0.0.1", 0)
    @port = @server.addr[1]
    @requests = []
    @rows = {}
    @thread = Thread.new do
      loop { handle(@server.accept) }
    rescue IOError, Errno::EBADF
      raise unless @server.closed?
    end
  end

  def handle(socket)
    path = socket.gets.split[1]
    headers = {}
    while (line = socket.gets) && line != "\r\n"
      name, value = line.split(":", 2)
      headers[name.downcase] = value.strip
    end
    body = JSON.parse(socket.read(Integer(headers.fetch("content-length"))))
    @requests << { path: path, headers: headers, body: body }
    status, answer = response || dispatch(path, body)
    payload = JSON.generate(answer)
    socket.write("HTTP/1.1 #{status} Fixture\r\nContent-Type: application/json\r\nContent-Length: #{payload.bytesize}\r\nConnection: close\r\n\r\n#{payload}")
  ensure
    socket.close
  end

  def dispatch(path, body)
    key = body.fetch("memory_key")
    case path
    when "/v1/memory.set"
      @rows[key] = body.fetch("value")
      [200, { status: "active", memory_id: "synthetic-id", version: 1, noop: false }]
    when "/v1/memory.get"
      [200, @rows.key?(key) ? { found: true, value: @rows[key] } : { found: false }]
    else
      raise "Unexpected fixture operation"
    end
  end

  def close
    @server.close
    @thread.value
  end
end

class RoastMemoryTest < Minitest::Test
  def setup
    @fixture = MemoryFixture.new
    @env = {
      "LOOM_BASE_URL" => "http://127.0.0.1:#{@fixture.port}", "LOOM_API_KEY" => "synthetic-fixture-token",
      "LOOM_ORG" => "example-org", "LOOM_NAMESPACE" => "example-memory", "LOOM_AGENT" => "release-helper",
      "LOOM_USER_ID" => "synthetic-user", "LOOM_SESSION_ID" => "synthetic-session"
    }
  end

  def teardown
    @fixture.close
  end

  def invoke(action = "recall", overrides = {})
    env = @env.merge("ROAST_MEMORY_ACTION" => action).merge(overrides)
    stdout, stderr, result = Timeout.timeout(30) do
      Open3.capture3(env, RbConfig.ruby, "-S", "bundle", "exec", "roast", "execute", "workflow.rb", chdir: __dir__)
    end
    refute_includes stdout + stderr, @env.fetch("LOOM_API_KEY")
    receipt = (stdout + stderr).lines.filter_map do |line|
      payload = line.split("ruby(:report) ❯ ", 2)[1]
      JSON.parse(payload) if payload
    end.last
    refute_nil receipt, "Missing receipt; exit=#{result.exitstatus}"
    [receipt, result, stdout + stderr]
  end

  def test_selected_fact_survives_two_workflow_processes
    receipt, result = invoke("save")
    assert result.success?
    assert_equal({ "status" => "saved", "noop" => false }, receipt)
    receipt, result = invoke
    assert result.success?
    assert_equal({ "status" => "found", "value" => RoastMemory::FACT }, receipt)
    assert_equal %w[/v1/memory.set /v1/memory.get], @fixture.requests.map { |r| r[:path] }
    @fixture.requests.each do |request|
      assert_equal "Bearer synthetic-fixture-token", request[:headers]["authorization"]
      assert_match(/loom-ruby\//, request[:headers]["x-loom-sdk"])
      RoastMemory::IDENTITY.each do |field|
        assert_equal @env.fetch("LOOM_#{field.upcase}"), request[:body][field]
      end
      refute request[:body].key?("actor")
    end
    saved, recalled = @fixture.requests.map { |r| r[:body] }
    assert_equal "derived", saved["source_class"]
    assert_equal RoastMemory::FACT, saved["value"]
    assert_equal saved["memory_key"], recalled["memory_key"]
    assert_match(%r{\Aroast-example/[0-9a-f]{64}\z}, saved["memory_key"])
    assert @fixture.requests.first[:headers].key?("idempotency-key")
    refute recalled.key?("value")
  end

  def test_missing_memory_abstains_and_each_identity_field_changes_the_key
    invoke("save")
    RoastMemory::IDENTITY.each do |field|
      receipt, result = invoke("recall", "LOOM_#{field.upcase}" => "other-#{field}")
      assert result.success?
      assert_equal({ "status" => "abstain" }, receipt)
    end
    assert_equal 6, @fixture.requests.map { |r| r[:body]["memory_key"] }.uniq.length
  end

  def test_configuration_and_action_refuse_before_http
    (@env.keys + ["ROAST_MEMORY_ACTION"]).each do |field|
      receipt, result = invoke("save", field => "")
      refute result.success?
      assert_equal({ "status" => "error", "reason" => "invalid_config" }, receipt)
    end
    ["http://remote.invalid", "https://user:password@example.invalid", "https://example.invalid/?secret=x"].each do |url|
      receipt, result = invoke("save", "LOOM_BASE_URL" => url)
      refute result.success?
      assert_equal "invalid_config", receipt["reason"]
    end
    assert_empty @fixture.requests
  end

  def test_backend_errors_and_invalid_receipts_never_report_a_save
    [[401, { detail: { code: "AUTH_DENIED", message: "synthetic-sensitive-body" } }],
     [503, { detail: { message: "synthetic-sensitive-body" } }],
     [200, { status: "conflicted", memory_id: "synthetic-id", version: 1, noop: false }],
     [200, {}]].each do |response|
      @fixture.response = response
      receipt, result, output = invoke("save")
      refute result.success?
      assert_equal "error", receipt["status"]
      refute_includes output, "synthetic-sensitive-body"
    end
    assert_equal 4, @fixture.requests.length
  end

  def test_false_missing_and_malformed_recall_are_distinct
    @fixture.response = [200, { found: false }]
    assert_equal({ "status" => "abstain" }, invoke.first)
    @fixture.response = [200, {}]
    receipt, result = invoke
    refute result.success?
    assert_equal({ "status" => "error", "reason" => "invalid_receipt" }, receipt)
  end
end
