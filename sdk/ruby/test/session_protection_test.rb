# frozen_string_literal: true

require "fileutils"
require "minitest/autorun"
require "tmpdir"
require_relative "../lib/clickhouse/loom"

class SessionProtectionTest < Minitest::Test
  def setup
    @root = Dir.mktmpdir
    @family = Loom::SessionProtection.create(File.join(@root, "family"))
    @sent = []
  end

  def teardown
    FileUtils.remove_entry(@root)
  end

  def client(family = @family, **options)
    Loom::Client.new(base_url: "https://loom.invalid", org: "o", namespace: "n", agent: "a",
                     session_protection: family, retry_policy: { max_attempts: 1 }, **options)
  end

  def response(status, body, media = "application/json")
    klass = Net::HTTPResponse::CODE_TO_OBJ.fetch(status.to_s)
    answer = klass.new("1.1", status.to_s, "response")
    answer["Content-Type"] = media
    answer.body = body
    answer.instance_variable_set(:@read, true)
    answer.define_singleton_method(:read_body) { |&block| block.call(body) }
    answer
  end

  def with_http(*answers, before: nil)
    sent = @sent
    fake = Object.new
    fake.define_singleton_method(:request) do |request, &block|
      before&.call
      sent << request
      answer = answers.shift
      raise answer if answer.is_a?(Exception)

      block&.call(answer)
      answer
    end
    Net::HTTP.stub(:start, ->(*_args, **_kwargs, &block) { block.call(fake) }) { yield }
  end

  def test_declaration_exists_only_with_installed_observer_and_store
    with_http(response(200, '{"found":false}'), response(200, '{"found":false}')) do
      client.get("k")
      client(nil).get("k")
    end
    assert_equal "v1", @sent[0]["X-Loom-Session-Protection"]
    assert_nil @sent[1]["X-Loom-Session-Protection"]
    refute @family.protected?
    refute @family.pending?
  end

  def test_error_metadata_latches_before_error_escapes_and_survives_reopen
    with_http(response(403, '{"error":{"data":{"mirrored":true}}}')) do
      assert_raises(Loom::Error) { client.call("knowledge.source.mcp_read", {}) }
    end
    assert @family.protected?
    assert Loom::SessionProtection.open(@family.pointer).protected?
  end

  def test_nested_text_envelope_and_typed_response_observation
    body = JSON.generate({ "content" => [{ "type" => "text", "text" => '{"items":[{"mirrored":true}]}' }] })
    with_http(response(200, body)) { client.get("k") }
    assert @family.protected?
  end

  def test_retained_callback_refuses_new_client_and_other_namespace
    capture = @family.capture
    with_http(response(200, '{"mirrored":true}')) { client.get("k") }
    assert_raises(Loom::AutomaticCaptureSuppressed) do
      client(nil, namespace: "other").call_automatic(capture, "memory.set", { value: "private" })
    end
    assert_equal 1, @sent.length
  end

  def test_clean_capture_cannot_omit_the_bound_clients_protection_or_pending_response
    [:protected, :pending].each do |state|
      bound = Loom::SessionProtection.create(File.join(@root, state.to_s))
      state == :protected ? bound.mark! : bound.begin_attempt
      Net::HTTP.stub(:start, ->(*_args, **_kwargs) { flunk "automatic dispatch opened a connection" }) do
        assert_raises(Loom::AutomaticCaptureSuppressed) do
          client(bound, namespace: "other").call_automatic(@family.capture, "memory.set", { value: "private" })
        end
      end
      assert_empty @sent
    end
  end

  def test_active_capture_observes_other_family_and_nested_callback_retains_it
    other = Loom::SessionProtection.create(File.join(@root, "other"))
    later = nil
    with_http(response(200, '{"mirrored":true}')) do
      @family.capture.call do
        later = other.capture
        client(other).get("k")
      end
    end
    assert @family.protected?
    assert other.protected?
    assert_raises(Loom::AutomaticCaptureSuppressed) { later.call { flunk "write escaped" } }
    assert_nil Loom::SessionCapture.current
  end

  def test_callback_failure_restores_ambient_scope
    assert_raises(RuntimeError) { @family.capture.call { raise "callback failed" } }
    assert_nil Loom::SessionCapture.current
    refute @family.protected?
  end

  def test_read_error_and_malformed_metadata_latch_without_returning_private_body
    ['{"mirrored":"false"}', '{"result":{"mirrored":null}}', 'private {'].each_with_index do |body, index|
      family = Loom::SessionProtection.create(File.join(@root, "bad-#{index}"))
      with_http(response(200, body)) do
        error = assert_raises(Loom::SessionProtectionUnavailable) { client(family).get("k") }
        refute_includes error.message, body
      end
      assert family.protected?
    end
  end

  def test_unsupported_stream_and_oversized_body_refuse
    [["data: private\n\n", "text/event-stream"], ["x" * (Loom::SessionProtection::MAX_BODY + 1), "application/json"]].each_with_index do |(body, media), index|
      family = Loom::SessionProtection.create(File.join(@root, "bounded-#{index}"))
      with_http(response(200, body, media)) do
        assert_raises(Loom::SessionProtectionUnavailable) { client(family).get("k") }
      end
      assert family.protected?
    end
  end

  def test_interrupted_body_keeps_pending_proof_and_latches
    answer = response(200, "")
    answer.define_singleton_method(:read_body) { |_block = nil| raise IOError, "interrupted" }
    with_http(answer) { assert_raises(Loom::Error) { client.get("k") } }
    assert @family.pending?
    assert Loom::SessionProtection.open(@family.pointer).protected?
  end

  def test_lost_state_and_interrupted_resume_never_create_clean_family
    pointer = @family.pointer
    @family.begin_attempt
    assert Loom::SessionProtection.open(pointer).protected?
    File.unlink(File.join(pointer["path"], "state"))
    assert_raises(Loom::SessionProtectionUnavailable) { Loom::SessionProtection.open(pointer) }
    assert_raises(Loom::SessionProtectionUnavailable) { client.get("k") }
    assert_empty @sent
  end

  def test_retry_barrier_runs_after_pacing_and_observes_error_status
    sleeper = ->(_delay) { @family.mark! }
    first = response(429, '{"detail":{"message":"wait"}}')
    first["Retry-After"] = "0.1"
    with_http(first, response(200, '{}')) do
      assert_raises(Loom::AutomaticCaptureSuppressed) do
        @family.capture.call { client(sleeper: sleeper).get("k") }
      end
    end
    assert_equal 1, @sent.length
  end

  def test_forged_protocol_header_refuses_before_post
    assert_raises(Loom::SessionProtectionUnavailable) do
      client.send(:post, "memory.get", "{}", { "X-Loom-Session-Protection" => "v1" })
    end
    assert_empty @sent
  end

  def test_cancellation_latches_and_keeps_pending_before_escape
    answer = response(200, "")
    answer.define_singleton_method(:read_body) { |_block = nil| raise Interrupt }
    with_http(answer) { assert_raises(Interrupt) { client.get("k") } }
    assert @family.protected?
    assert @family.pending?
  end

  def test_monotonic_local_latch_rejects_state_rollback
    @family.mark!
    File.write(File.join(@family.pointer["path"], "state"), "0")
    assert_raises(Loom::SessionProtectionUnavailable) { @family.ready! }
  end
end
