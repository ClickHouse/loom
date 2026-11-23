# frozen_string_literal: true

require "fileutils"
require "tmpdir"
require "timeout"
require_relative "test_helper"

# One owned worker handles the sequential complete-response cases.
class ProtectionHTTPServer < FakeServer
  def close
    @server.close unless @server.closed?
    raise "HTTP server did not stop" unless @thread.join(4)
    @thread.value
  end

  private

  def serve
    loop do
      socket = @server.accept
      Timeout.timeout(3) { handle(socket) }
    end
  rescue IOError, Errno::EBADF
    raise unless @server.closed?
  end
end

# Complete responses use FakeServer. This variant holds one chunked response so
# cancellation can prove that Net::HTTP closes the connection it owns.
class ProtectionChunkServer < FakeServer
  attr_reader :chunk_sent, :peer_closed

  def initialize(interrupt: false)
    @interrupt = interrupt
    @chunk_sent = Queue.new
    @peer_closed = Queue.new
    @socket_lock = Mutex.new
    @socket = nil
    super()
  end

  def close
    @server.close unless @server.closed?
    @socket_lock.synchronize { @socket&.close unless @socket&.closed? }
    raise "chunk server did not stop" unless @thread.join(3)
    @thread.value
  end

  private

  def serve
    socket = @server.accept
    @socket_lock.synchronize { @socket = socket }
    Timeout.timeout(3) do
      method, path, = socket.gets.split
      headers = {}
      while (line = socket.gets) && line != "\r\n"
        name, value = line.split(":", 2)
        headers[name.downcase] = value.strip
      end
      body = socket.read(Integer(headers.fetch("content-length")))
      @requests << Request.new(method, path, headers, body)
      socket.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
      chunk = '{"result":{"mirrored":true,"private":"'
      socket.write("#{chunk.bytesize.to_s(16)}\r\n#{chunk}\r\n")
      socket.flush
      @chunk_sent << true
      if @interrupt
        socket.close
      else
        @peer_closed << socket.read(1).nil?
      end
    end
  rescue IOError, Errno::EBADF
    raise unless @server.closed?
  ensure
    socket&.close unless socket&.closed?
  end
end

class SessionProtectionHTTPTest < Minitest::Test
  Loom = ClickHouse::Loom
  SOURCE_READ = "knowledge.source.mcp_read"
  PRIVATE_VALUE = "synthetic-private-source-value"

  def setup
    @server = ProtectionHTTPServer.new
    @root = Dir.mktmpdir("loom-sdk-wire-")
    @family = family("bound")
  end

  def teardown
    @server&.close
  ensure
    FileUtils.remove_entry(@root) if @root
  end

  def family(name)
    Loom::SessionProtection.create(File.join(@root, name))
  end

  def protected_client(bound = @family, **options)
    defaults = { base_url: @server.base_url, org: "dev", namespace: "local", agent: "assistant",
                 retry_policy: { max_attempts: 1 }, rate_limit_max_wait: 0,
                 session_protection: bound, api_key: "synthetic-token", open_timeout: 2, read_timeout: 2 }
    Loom::Client.new(**defaults, **options)
  end

  def json_response(status, value, headers = {})
    @server.respond(status, JSON.generate(value), { "Content-Type" => "application/json" }.merge(headers))
  end

  def read_private(reader = protected_client)
    json_response(200, { "result" => { "mirrored" => true, "value" => PRIVATE_VALUE } })
    answer = reader.call(SOURCE_READ, source_body)
    assert_equal PRIVATE_VALUE, answer.fetch("result").fetch("value")
    assert_wire(@server.requests.last, SOURCE_READ, "local", "synthetic-token")
  end

  def assert_wire(request, op, namespace, token, bound: true)
    assert_equal "POST", request.method
    assert_equal "/v1/#{op}", request.path
    assert_equal "Bearer #{token}", request.headers.fetch("authorization")
    assert_equal "application/json", request.headers.fetch("content-type")
    if bound
      assert_equal "v1", request.headers.fetch("x-loom-session-protection")
    else
      refute request.headers.key?("x-loom-session-protection")
    end
    expected = (op == SOURCE_READ ? source_body : write_body).transform_keys(&:to_s)
    expected.merge!("org" => "dev", "namespace" => namespace, "agent" => "assistant")
    assert_equal expected, JSON.parse(request.body)
  end

  def source_body
    { name: "synthetic-source", resource_id: "synthetic-resource" }
  end

  def write_body
    { memory_key: "wire-key", value: "explicit-value" }
  end

  def test_actual_source_read_reopens_and_suppresses_clean_retained_capture_on_other_clients
    clean = family("clean")
    retained = clean.capture
    reader = protected_client
    json_response(200, { "status" => "ok" })
    assert_equal "ok", reader.call_automatic(retained, "memory.set", write_body).fetch("status")
    assert_wire(@server.requests.last, "memory.set", "local", "synthetic-token")
    read_private(reader)
    refute clean.protected?
    refute clean.pending?
    reopened = Loom::SessionProtection.open(@family.pointer)
    assert reopened.protected?
    refute @family.pending?
    [reader.dup, protected_client(reopened, namespace: "other", api_key: "other-token")].each do |writer|
      before = @server.requests.length
      assert_raises(Loom::AutomaticCaptureSuppressed) do
        writer.call_automatic(retained, "memory.set", write_body)
      end
      assert_equal before, @server.requests.length
    end
    json_response(200, { "status" => "ok" })
    assert_equal "ok", reader.call("memory.set", write_body).fetch("status")
    assert_wire(@server.requests.last, "memory.set", "local", "synthetic-token")
    json_response(200, { "status" => "ok" })
    ordinary = protected_client(nil, namespace: "ordinary", api_key: "ordinary-token")
    assert_equal "ok", ordinary.call("memory.set", write_body).fetch("status")
    assert_wire(@server.requests.last, "memory.set", "ordinary", "ordinary-token", bound: false)
  end

  def test_actual_error_metadata_is_durable_before_typed_error_escapes
    json_response(403, { "detail" => { "code" => "FORBIDDEN", "message" => "denied" },
                         "error" => { "data" => { "mirrored" => true, "value" => PRIVATE_VALUE } } })
    error = assert_raises(Loom::Error) { protected_client.call(SOURCE_READ, source_body) }
    assert_equal 403, error.status
    assert Loom::SessionProtection.open(@family.pointer).protected?
    refute @family.pending?
    assert_wire(@server.requests.last, SOURCE_READ, "local", "synthetic-token")
  end

  def test_actual_chunked_interruption_and_cancellation_keep_proof_and_close_connections
    [true, false].each do |interrupt|
      stream_family = family("stream-#{interrupt}")
      server = ProtectionChunkServer.new(interrupt: interrupt)
      reader = protected_client(stream_family, base_url: server.base_url)
      worker = Thread.new do
        begin
          reader.call(SOURCE_READ, source_body)
        rescue Loom::Error, Loom::SessionProtectionUnavailable => error
          error
        end
      end
      worker.report_on_exception = false
      Timeout.timeout(3) { server.chunk_sent.pop }
      unless interrupt
        assert_nil worker.join(0.05), "incomplete chunked body unexpectedly completed"
        worker.kill
      end
      assert worker.join(3), "request worker did not stop"
      assert_kind_of StandardError, worker.value if interrupt
      assert Timeout.timeout(3) { server.peer_closed.pop } unless interrupt
      assert Loom::SessionProtection.open(stream_family.pointer).protected?
      assert stream_family.pending?
      assert_wire(server.requests.fetch(0), SOURCE_READ, "local", "synthetic-token")
      assert_raises(Loom::AutomaticCaptureSuppressed) do
        reader.call_automatic(family("stream-clean-#{interrupt}").capture, "memory.set", write_body)
      end
      assert_equal 1, server.requests.length
    ensure
      worker&.kill if worker&.alive?
      worker&.join(3)
      server&.close
    end
  end

  def test_actual_retry_and_pacing_recheck_protection_acquired_after_first_response
    [429, 503].each do |status|
      bound = family("wait-bound-#{status}")
      clean = family("wait-clean-#{status}")
      reader = protected_client(bound)
      before = @server.requests.length
      sleeps = []
      code = status == 429 ? "RATE_LIMITED" : "INTERNAL_ERROR"
      json_response(status, { "detail" => { "code" => code, "message" => "wait" } }, "Retry-After" => "1")
      writer = protected_client(bound, rate_limit_max_wait: 2, retry_policy: { max_attempts: 2 },
                                sleeper: lambda { |delay|
        sleeps << delay
        read_private(reader)
      })
      assert_raises(Loom::AutomaticCaptureSuppressed) do
        writer.call_automatic(clean.capture, "memory.set", write_body)
      end
      assert_equal [1.0], sleeps
      requests = @server.requests.drop(before)
      assert_equal ["/v1/memory.set", "/v1/#{SOURCE_READ}"], requests.map(&:path)
      assert_wire(requests.first, "memory.set", "local", "synthetic-token")
      assert Loom::SessionProtection.open(bound.pointer).protected?
    end
  end

  def test_lost_or_changed_pointer_refuses_before_actual_http
    [:lost, :changed].each do |mode|
      bound = family(mode.to_s)
      writer = protected_client(bound)
      pointer = bound.pointer
      if mode == :lost
        File.unlink(File.join(pointer.fetch("path"), "state"))
      else
        File.write(File.join(pointer.fetch("path"), "identity"), "00000000-0000-0000-0000-000000000000")
      end
      assert_raises(Loom::SessionProtectionUnavailable) { Loom::SessionProtection.open(pointer) }
      assert_raises(Loom::SessionProtectionUnavailable) { writer.call("memory.set", write_body) }
      assert_empty @server.requests
    end
  end
end
