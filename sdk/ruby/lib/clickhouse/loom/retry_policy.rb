# frozen_string_literal: true

require "time"

module ClickHouse
  module Loom
    # When a call is sent again, the same policy as the TypeScript and Python SDKs:
    #
    # - a connection that failed before the request went out is resent;
    # - an HTTP status in +statuses+ (408 and 5xx) is resent for an op the registry
    #   marks idempotent, and for any other op only on a 408 or a refusal the server
    #   makes before running it (503 AUTH_UNAVAILABLE), since a 5xx after the handler
    #   started may follow a write;
    # - a timeout or a response cut off after sending is resent only for an op whose
    #   Idempotency-Key the server deduplicates (idempotent and not read-only), unless
    #   +retry_timeouts+ says :never or :always;
    # - a 429 is not a retry but pacing: the client waits out the server's Retry-After
    #   for at most the client's +rate_limit_max_wait+ seconds per call.
    #
    # A retried answer's Retry-After (or its detail.retry_after_s) replaces the backoff
    # when it is at most +max_retry_after+ seconds. Every resend carries the same
    # Idempotency-Key and X-Request-Id as the first attempt.
    RetryPolicy = Struct.new(:max_attempts, :backoff, :backoff_max, :backoff_jitter, :statuses,
                             :retry_connection_errors, :retry_timeouts, :respect_retry_after,
                             :max_retry_after, :total_backoff_budget, keyword_init: true) do
      # Total attempts including the first (1 disables retries); the first backoff in
      # seconds, doubled per attempt up to +backoff_max+; +backoff_jitter+ 0..1 spreads
      # each sleep over delay * (1 - jitter/2 + rand * jitter); +total_backoff_budget+
      # bounds the seconds one call may sleep between attempts.
      def self.default
        new(max_attempts: 3, backoff: 0.25, backoff_max: 5.0, backoff_jitter: 0.5,
            statuses: [408, *500..599].freeze, retry_connection_errors: true, retry_timeouts: :idempotent,
            respect_retry_after: true, max_retry_after: RetryAfter::MAX_SECONDS,
            total_backoff_budget: Float::INFINITY).freeze
      end

      # +base+ with the fields of +override+ (a Hash) set, each one checked. Raises
      # ArgumentError naming the field and +source+.
      def self.resolve(base, override, source = "Client")
        return base if override.nil? || override.empty?
        raise ArgumentError, "#{source}: retry_policy must be a Hash" unless override.is_a?(Hash)

        policy = base.dup
        override.each { |name, value| policy[check_name(name, source)] = check(name.to_sym, value, source) }
        policy.freeze
      end

      def self.check_name(name, source)
        return name.to_sym if members.include?(name.to_sym)

        raise ArgumentError, "#{source}: unknown retry_policy field #{name.inspect} (known: #{members.join(", ")})"
      end
      private_class_method :check_name

      def self.check(name, value, source)
        case name
        when :max_attempts
          return value if value.is_a?(Integer) && value >= 1
        when :backoff_jitter
          return value.to_f if value.is_a?(Numeric) && value.between?(0, 1)
        when :backoff, :backoff_max, :max_retry_after, :total_backoff_budget
          return value.to_f if value.is_a?(Numeric) && value >= 0
        when :statuses
          list = Array(value)
          return list.freeze if list.all? { |s| s.is_a?(Integer) && s.between?(100, 599) }
        when :retry_timeouts
          return value.to_sym if %w[never idempotent always].include?(value.to_s)
        else # retry_connection_errors, respect_retry_after
          return value if [true, false].include?(value)
        end
        raise ArgumentError, "#{source}: retry_policy #{name} cannot be #{value.inspect}"
      end
      private_class_method :check

      # Seconds to sleep before attempt +attempt+ + 1 (+attempt+ is the 1-based one that failed).
      def backoff_delay(attempt, random = rand)
        exponential = [backoff * (2**(attempt - 1)), backoff_max].min
        exponential * (1 - (backoff_jitter / 2.0) + (random * backoff_jitter))
      end
    end

    # Reads a Retry-After header: a number of seconds ("2", "0.5") or an HTTP-date.
    module RetryAfter
      # The longest wait believed; a 429 asking for longer is waited out for DEFAULT_SECONDS.
      MAX_SECONDS = 60.0
      DEFAULT_SECONDS = 1.0

      module_function

      # Seconds to wait from +now+, or nil when +value+ is absent or unreadable. A past
      # date or a negative number is 0.
      def parse(value, now = Time.now)
        raw = value.to_s.strip
        return nil if raw.empty?
        return [Float(raw), 0.0].max if raw.match?(/\A[+-]?\d+(\.\d+)?\z/)

        [Time.httpdate(raw) - now, 0.0].max
      rescue ArgumentError
        nil
      end

      # The bounded wait a 429 asks for.
      def for_rate_limit(value, now = Time.now)
        wait = parse(value, now)
        wait.nil? || wait > MAX_SECONDS ? DEFAULT_SECONDS : wait
      end
    end
  end
end
