# frozen_string_literal: true

require_relative "test_helper"

class RetryTest < LoomTest
  def setup
    super
    @sleeps = []
  end

  # Three attempts, no jitter, and sleeps recorded instead of slept.
  def retrying(**options)
    client(retry_policy: { max_attempts: 3, backoff_jitter: 0 }, rate_limit_max_wait: 15,
           sleeper: ->(seconds) { @sleeps << seconds }, **options)
  end

  def unavailable(code = "STORE_UNAVAILABLE", **detail)
    { detail: { code: code, message: "busy", category: "dependency", retryable: true, **detail } }
  end

  def test_an_idempotent_op_is_resent_after_a_5xx_with_exponential_backoff
    respond_json(503, unavailable)
    respond_json(502, unavailable)
    respond_json(200, { found: false })

    out = retrying.memory.get(memory_key: "k")

    assert_equal({ "found" => false }, out)
    assert_equal 3, @server.requests.size
    assert_equal [0.25, 0.5], @sleeps
    assert_equal %w[0 1 2], @server.requests.map { |r| r.headers["x-loom-retry-count"] }
  end

  def test_every_resend_repeats_the_idempotency_key_and_the_request_id
    respond_json(500, { detail: { code: "INTERNAL_ERROR", message: "boom" } })
    respond_json(503, unavailable)
    respond_json(200, { memory_id: "m1" })

    retrying.memory.set(memory_key: "k", value: "v")

    keys = @server.requests.map { |r| r.headers["idempotency-key"] }
    ids = @server.requests.map { |r| r.headers["x-request-id"] }

    assert_equal 3, keys.size
    assert_equal 1, keys.uniq.size
    refute_nil keys.first
    assert_equal 1, ids.uniq.size
  end

  def test_a_retry_after_header_replaces_the_backoff
    respond_json(503, unavailable, { "Retry-After" => "2" })
    respond_json(200, {})

    retrying.memory.get(memory_key: "k")

    assert_equal [2.0], @sleeps
  end

  def test_the_body_retry_after_s_is_honoured_when_no_header_names_a_wait
    respond_json(503, unavailable(retry_after_s: 1.5))
    respond_json(200, {})

    retrying.memory.get(memory_key: "k")

    assert_equal [1.5], @sleeps
  end

  def test_a_retry_after_longer_than_the_policy_believes_falls_back_to_the_backoff
    respond_json(503, unavailable, { "Retry-After" => "600" })
    respond_json(200, {})

    retrying.memory.get(memory_key: "k")

    assert_equal [0.25], @sleeps
  end

  def test_the_last_answer_is_raised_once_the_attempts_are_spent
    3.times { respond_json(503, unavailable) }

    error = assert_raises(Loom::Error) { retrying.memory.get(memory_key: "k") }

    assert_equal "STORE_UNAVAILABLE", error.code
    assert_equal 3, @server.requests.size
    assert_equal 2, @sleeps.size
  end

  def test_an_op_that_is_not_idempotent_is_not_resent_after_a_5xx_and_is_not_retryable
    respond_json(500, { detail: { code: "INTERNAL_ERROR", message: "boom" } })

    error = assert_raises(Loom::InternalError) { retrying.assistant.ask(question: "q") }

    assert_equal 1, @server.requests.size
    refute error.retryable?
    refute @server.requests.first.headers.key?("idempotency-key")
  end

  def test_an_op_that_is_not_idempotent_is_resent_after_a_refusal_made_before_it_ran
    respond_json(503, { detail: { code: "AUTH_UNAVAILABLE", message: "idp down" } })
    respond_json(200, { answer: "a" })

    assert_equal({ "answer" => "a" }, retrying.assistant.ask(question: "q"))
    assert_equal 2, @server.requests.size
  end

  def test_a_configuration_error_and_a_never_retried_code_are_not_resent
    respond_json(503, { detail: { code: "SCHEMA_PENDING", message: "migrate", category: "configuration",
                                  retryable: false } })
    respond_json(503, { detail: { code: "PUBLICATION_PENDING", message: "committed" } })

    assert_raises(Loom::SchemaPending) { retrying.memory.get(memory_key: "k") }
    error = assert_raises(Loom::PublicationPending) { retrying.memory.get(memory_key: "k") }

    assert_equal 2, @server.requests.size
    assert_equal "dependency", error.category
  end

  def test_a_4xx_is_not_resent
    respond_json(409, { detail: { code: "PINNED_CONFLICT", message: "pinned" } })

    assert_raises(Loom::PinnedConflict) { retrying.memory.set(memory_key: "k", value: "v") }
    assert_equal 1, @server.requests.size
  end

  def test_a_429_is_waited_out_for_its_retry_after_and_not_counted_as_an_attempt
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "3" })
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "3" })
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "3" })
    respond_json(200, { memory_id: "m1" })

    retrying(retry_policy: { max_attempts: 1 }).memory.set(memory_key: "k", value: "v")

    assert_equal [3.0, 3.0, 3.0], @sleeps
    assert_equal 1, @server.requests.map { |r| r.headers["idempotency-key"] }.uniq.size
  end

  def test_a_429_past_the_wait_budget_raises_rate_limited_with_its_wait
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "10" })
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "10" })

    error = assert_raises(Loom::RateLimited) { retrying.memory.get(memory_key: "k") }

    assert_equal [10.0], @sleeps
    assert_in_delta 10.0, error.retry_after
    assert error.retryable?
  end

  def test_rate_limit_max_wait_zero_raises_the_first_429
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow" } }, { "Retry-After" => "1" })

    assert_raises(Loom::RateLimited) { retrying(rate_limit_max_wait: 0).memory.get(memory_key: "k") }
    assert_empty @sleeps
  end

  def test_a_refused_connection_is_resent_and_then_raised_as_a_network_error
    sleeps = []
    c = Loom::Client.new(base_url: "http://127.0.0.1:1", open_timeout: 2, sleeper: ->(s) { sleeps << s },
                         retry_policy: { max_attempts: 3, backoff_jitter: 0 })

    error = assert_raises(Loom::Error) { c.memory.set(memory_key: "k", value: "v") }

    assert_equal Loom::CODE_NETWORK, error.code
    assert_equal [0.25, 0.5], sleeps
  end

  def test_a_timeout_is_resent_only_for_an_op_the_server_dedups
    @server.respond(200, "{}", {}, delay: 0.6)
    @server.respond(200, "{}", {}, delay: 0.6)
    @server.respond(200, JSON.generate({ memory_id: "m1" }))

    get_error = assert_raises(Loom::Error) { retrying(read_timeout: 0.3).memory.get(memory_key: "k") }
    out = retrying(read_timeout: 0.3).memory.set(memory_key: "k", value: "v")

    assert_equal Loom::CODE_NETWORK, get_error.code
    assert_equal [0.25], @sleeps
    assert_equal({ "memory_id" => "m1" }, out)
  end

  def test_a_per_call_policy_overrides_the_client_one
    respond_json(503, unavailable)

    assert_raises(Loom::Error) do
      retrying.memory.get(memory_key: "k", request_options: { retry_policy: { max_attempts: 1 } })
    end
    assert_equal 1, @server.requests.size
  end

  def test_a_policy_field_is_checked_by_name
    error = assert_raises(ArgumentError) { client(retry_policy: { max_attempts: 0 }) }
    assert_includes error.message, "max_attempts"

    error = assert_raises(ArgumentError) { client(retry_policy: { attempts: 2 }) }
    assert_includes error.message, "unknown retry_policy field :attempts"
  end

  def test_retry_after_reads_seconds_and_http_dates
    now = Time.utc(2026, 11, 22, 10, 0, 0)

    assert_in_delta 0.5, Loom::RetryAfter.parse("0.5", now)
    assert_in_delta 30.0, Loom::RetryAfter.parse("Sun, 22 Nov 2026 10:00:30 GMT", now)
    assert_in_delta 0.0, Loom::RetryAfter.parse("Sun, 22 Nov 2026 09:00:00 GMT", now)
    assert_nil Loom::RetryAfter.parse("soon", now)
    assert_in_delta 1.0, Loom::RetryAfter.for_rate_limit("3600", now)
  end
end
