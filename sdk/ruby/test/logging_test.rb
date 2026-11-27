# frozen_string_literal: true

require_relative "test_helper"

class LoggingTest < LoomTest
  KEY = "lk_live_0123456789abcd1234"

  # Records each line with its level, as a ::Logger would receive it.
  class Capture
    attr_reader :lines

    def initialize
      @lines = []
    end

    %i[debug info warn error].each do |level|
      define_method(level) { |message| @lines << [level, message] }
    end

    def at(level)
      @lines.select { |l, _| l == level }.map(&:last)
    end
  end

  def setup
    super
    @saved_level = ENV.fetch("LOOM_LOG_LEVEL", nil)
    ENV.delete("LOOM_LOG_LEVEL")
    @log = Capture.new
  end

  def teardown
    @saved_level ? ENV["LOOM_LOG_LEVEL"] = @saved_level : ENV.delete("LOOM_LOG_LEVEL")
    super
  end

  # A 503 then a 200 for the first call, a 404 for the second: the same script as the
  # TypeScript SDK's logging test.
  def run_script(level)
    respond_json(503, { detail: { code: "OVERLOADED", message: "busy", category: "capacity", retryable: true } })
    respond_json(200, { found: false }, { "X-Request-Id" => "rid-7" })
    respond_json(404, { detail: { code: "RESOURCE_NOT_FOUND", message: "no" } })
    loom = client(api_key: KEY, logger: @log, log_level: level, retry_policy: { max_attempts: 3, backoff_jitter: 0 },
                  sleeper: ->(_) {})
    loom.memory.get(memory_key: "k")
    assert_raises(Loom::Error) { loom.memory.get(memory_key: "k") }
    @log.lines
  end

  def test_the_raw_api_key_is_never_logged_at_any_level
    %i[debug info warn error].each do |level|
      @log = Capture.new
      lines = run_script(level)

      refute_empty lines, level
      lines.each { |_, text| refute_includes text, KEY, level }
    end
  end

  def test_one_line_per_call_at_info_retries_at_warn_the_failure_at_error_headers_redacted_at_debug
    run_script(:debug)

    info = @log.at(:info)
    assert_equal 1, info.size
    assert_match(/\Amemory\.get 200 \d+ ms attempts=2 rid=rid-7\z/, info.first)
    assert_match(%r{\Amemory\.get attempt 1/3 503 OVERLOADED; retrying in 250 ms \(rid=}, @log.at(:warn).first)
    assert_match(/\Amemory\.get failed after \d+ ms: ResourceNotFound RESOURCE_NOT_FOUND \(404\) \(rid=/,
                 @log.at(:error).first)
    debug = @log.at(:debug).first
    assert_includes debug, "Bearer …1234"
    assert_match(%r{memory\.get → POST http://127\.0\.0\.1:\d+/v1/memory\.get attempt 1 waits 60000 ms bytes \d+},
                 debug)
  end

  def test_the_level_drops_the_lines_below_it
    run_script(:error)

    assert_equal [:error], @log.lines.map(&:first).uniq
  end

  def test_no_logger_logs_nothing_and_the_default_level_is_warn
    respond_json(200, {})
    client.memory.get(memory_key: "k") # no logger: nothing to write to
    respond_json(200, {})
    client(logger: @log).memory.get(memory_key: "k")

    assert_empty @log.lines # info is below the default warn
  end

  def test_log_level_off_logs_nothing
    respond_json(404, { detail: { code: "RESOURCE_NOT_FOUND", message: "no" } })

    assert_raises(Loom::Error) { client(logger: @log, log_level: :off).memory.get(memory_key: "k") }
    assert_empty @log.lines
  end

  def test_loom_log_level_sets_the_level_and_takes_python_names
    ENV["LOOM_LOG_LEVEL"] = "INFO"
    respond_json(200, {})
    client(logger: @log).memory.get(memory_key: "k")

    assert_equal 1, @log.at(:info).size
    assert_equal :error, Loom::Logging.parse_level("CRITICAL", "LOOM_LOG_LEVEL")
    assert_equal :warn, Loom::Logging.parse_level(" warning ", "LOOM_LOG_LEVEL")
  end

  def test_an_unreadable_loom_log_level_warns_and_keeps_the_default
    ENV["LOOM_LOG_LEVEL"] = "loud"

    client(logger: @log)

    assert_equal ['LOOM_LOG_LEVEL: invalid log level "loud"; expected one of debug, info, warn, error, off; using warn'],
                 @log.at(:warn)
  end

  def test_an_unreadable_log_level_argument_raises
    error = assert_raises(ArgumentError) { client(logger: @log, log_level: "loud") }
    assert_match(/log_level: invalid log level "loud"/, error.message)
  end

  def test_the_configured_logger_and_level_are_used
    Loom.configure do |c|
      c.logger = @log
      c.log_level = :info
    end
    respond_json(200, {})
    client.memory.get(memory_key: "k")

    assert_equal 1, @log.at(:info).size
  end

  def test_a_429_wait_is_logged_at_warn
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow down" } }, { "Retry-After" => "2" })
    respond_json(200, {})
    client(logger: @log, rate_limit_max_wait: 15, sleeper: ->(_) {}).memory.get(memory_key: "k")

    assert_match(/\Amemory\.get 429 rate limited; waiting 2000 ms \(rid=/, @log.at(:warn).first)
  end

  def test_redact_headers_keeps_the_scheme_and_the_last_four_characters
    assert_equal({ "Authorization" => "Bearer …1234", "Accept" => "application/json", "X-Api-Key" => "…1234",
                   "Cookie" => "…" },
                 Loom::Logging.redact_headers("Authorization" => "Bearer #{KEY}", "Accept" => "application/json",
                                              "X-Api-Key" => KEY, "Cookie" => "short"))
  end

  def test_a_token_in_a_message_is_redacted_before_it_reaches_the_logger
    leveled = Loom::Logging.logger_for(@log, :debug)
    leveled.warn("retrying with Authorization: Bearer #{KEY}")

    assert_equal ["retrying with Authorization: Bearer …1234"], @log.at(:warn)
  end
end
