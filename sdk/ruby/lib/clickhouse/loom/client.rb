# frozen_string_literal: true

require "json"
require "net/http"
require "openssl"
require "securerandom"
require "uri"

require_relative "errors"
require_relative "version"
require_relative "configuration"
require_relative "logging"
require_relative "retry_policy"
require_relative "operations"
require_relative "pagination"
require_relative "generated/errors"
require_relative "generated/ops"

module ClickHouse
  module Loom
    # The server used when neither base_url: nor LOOM_BASE_URL is set.
    DEFAULT_BASE_URL = "http://localhost:7777"

    SDK_IDENT = "loom-ruby/#{VERSION}"

    # What memory.set answers.
    SetResult = Struct.new(:memory_id, :version, :content_sha256, :status, :noop, keyword_init: true)

    # What memory.get answers. +found+ is false when no live row holds the key; the
    # other fields are then empty.
    Memory = Struct.new(:found, :memory_id, :value, :memory_type, :version, :tags, keyword_init: true)

    # One ranked memory of a search.
    SearchHit = Struct.new(:memory_key, :memory_type, :content_excerpt, :similarity, :version, keyword_init: true)

    # What memory.search answers: the ranked +results+, best first.
    SearchResult = Struct.new(:results, keyword_init: true)

    # What memory.delete answers. +found+ is false when no live row of the key exists
    # in the namespace; +tombstoned+ is true when a tombstone was written over one.
    DeleteResult = Struct.new(:found, :tombstoned, :version, keyword_init: true)

    # Calls every Loom operation as one org, namespace and agent.
    #
    # client.memory.set(memory_key: "k", value: "v") posts /v1/memory.set: one method per
    # served op, generated from the server's registry (Generated::OPS) and grouped by the
    # op name's first segment. The org, namespace and agent are bound once and sent with
    # every call; a keyword of the same name overrides one for that call, and a blank one
    # is left out of the body, so the server's default applies. set, get, search and
    # delete are the hand-written memory calls with typed answers.
    #
    # A call is resent as RetryPolicy describes, a 429 is waited out, and an op the server
    # deduplicates gets one Idempotency-Key that every resend repeats. The client does not
    # follow redirects, so a memory body and token never go to another origin: a 3xx
    # answer raises an Error.
    class Client
      include Generated::GroupAccessors

      # Raised before the request went out: resending cannot run the op twice.
      UNSENT_ERRORS = [Errno::ECONNREFUSED, Errno::EHOSTUNREACH, Errno::ENETUNREACH, Errno::EADDRNOTAVAIL,
                       SocketError, Net::OpenTimeout].freeze
      NETWORK_ERRORS = [SystemCallError, SocketError, IOError, Timeout::Error, Net::ProtocolError,
                        OpenSSL::SSL::SSLError].freeze

      # The bound identity sent with every call ("org", "namespace", "agent"; blank ones left out).
      attr_reader :identity
      # The server every call goes to, and the resolved RetryPolicy.
      attr_reader :base_url, :retry_policy

      # Each argument left nil takes ClickHouse::Loom.configure's value, then the environment
      # (LOOM_BASE_URL, LOOM_API_KEY), then the default: http://localhost:7777 and no
      # Authorization header, which a server with auth off accepts. +retry_policy+ is a
      # Hash of RetryPolicy fields set over the configured policy ({ max_attempts: 1 }
      # disables retries); +rate_limit_max_wait+ is the seconds one call may spend waiting
      # out 429 answers (default 15, 0 raises the first). +logger+ and +log_level+ turn on
      # logging (see Logging; off unless a logger is given). +sleeper+ is called with the
      # seconds to wait between attempts (a test seam; default Kernel#sleep).
      def initialize(base_url: nil, api_key: nil, org: nil, namespace: nil, agent: nil, open_timeout: nil,
                     read_timeout: nil, retry_policy: nil, rate_limit_max_wait: nil, logger: nil, log_level: nil,
                     sleeper: nil)
        config = Loom.configuration
        @base_url = first_non_blank(base_url, config.base_url, ENV.fetch("LOOM_BASE_URL", nil),
                                    DEFAULT_BASE_URL).sub(%r{/+\z}, "")
        # An exported but empty LOOM_API_KEY must not send "Bearer " and turn a clear 401 into a puzzle.
        @api_key = first_non_blank(api_key, config.api_key, ENV.fetch("LOOM_API_KEY", nil))
        @identity = { "org" => first_non_blank(org, config.org),
                      "namespace" => first_non_blank(namespace, config.namespace),
                      "agent" => first_non_blank(agent, config.agent) }.compact.freeze
        @open_timeout = open_timeout || config.open_timeout || 10
        @read_timeout = read_timeout || config.read_timeout || 60
        @retry_policy = RetryPolicy.resolve(RetryPolicy.default, config.retry_policy, "ClickHouse::Loom.configure")
        @retry_policy = RetryPolicy.resolve(@retry_policy, retry_policy, "Client")
        @rate_limit_max_wait = Float(rate_limit_max_wait || config.rate_limit_max_wait || 15)
        @sleeper = sleeper || ->(seconds) { sleep(seconds) }
        @log = Logging.logger_for(logger || config.logger, log_level || config.log_level)
        @groups = {}
      end

      # The ops of one group: client.op_group("memory") is client.memory.
      def op_group(name)
        @groups[name.to_s] ||= Generated::GROUPS.fetch(name.to_s).new(self)
      end

      # Posts +input+ (a Hash) over the bound identity to /v1/<op> and returns the decoded
      # answer. An op the generated table does not know (a newer server) is sent as it is:
      # no Idempotency-Key unless one is given, and resent only when it provably never ran.
      def call(op, input = nil, idempotency_key: nil, retry_policy: nil)
        spec = Generated::OPS[op]
        body = JSON.generate(@identity.merge((input || {}).transform_keys(&:to_s)))
        policy = RetryPolicy.resolve(@retry_policy, retry_policy, "call #{op}")
        send_with_retries(op, spec, body, request_headers(spec, idempotency_key), policy)
      end

      # Walks a cursor-paginated op page by page and returns a lazy Enumerator of its rows;
      # see Pagination.paginate. The block fetches one page for the cursor it is given:
      #
      #   client.paginate { |cursor| client.knowledge.list(limit: 50, cursor: cursor) }.first(200)
      def paginate(max_pages: nil, &fetch_page)
        Pagination.paginate(max_pages: max_pages, &fetch_page)
      end

      # Saves +value+ under +key+: plain text, or any Hash, Array or scalar. Writing a
      # key that already holds a row adds a new version. +memory_type+ left nil lets the
      # server use "semantic".
      def set(key, value, memory_type: nil, tags: nil)
        body = { memory_key: key, value: value, memory_type: memory_type, tags: tags }.compact
        out = call("memory.set", body)
        SetResult.new(memory_id: out["memory_id"], version: out["version"],
                      content_sha256: out["content_sha256"], status: out["status"], noop: out["noop"] == true)
      end

      # Reads the live row of +key+: under the client's agent first, then the same
      # user's row under any agent. A missing key is not an error: +found+ is false.
      def get(key)
        out = call("memory.get", { memory_key: key })
        Memory.new(found: out["found"] == true, memory_id: out["memory_id"], value: out["value"],
                   memory_type: out["memory_type"], version: out["version"], tags: Array(out["tags"]))
      end

      # Ranks the memories that answer +query+. With no agent bound, it reads every
      # agent's rows in the namespace; with one, only that agent's. +top_k+ left nil lets
      # the server use 5; +tags+ keeps only rows carrying any of them. With no argument at
      # all it is the search.* group (client.search.query(...)), like every other group.
      def search(query = nil, top_k: nil, memory_type: nil, tags: nil)
        return op_group("search") if [query, top_k, memory_type, tags].all?(&:nil?)

        body = { query: query, top_k: top_k, memory_type: memory_type, tags: tags }.compact
        out = call("memory.search", body)
        hits = Array(out["results"]).map do |h|
          SearchHit.new(memory_key: h["memory_key"], memory_type: h["memory_type"],
                        content_excerpt: h["content_excerpt"], similarity: h["similarity"], version: h["version"])
        end
        SearchResult.new(results: hits)
      end

      # Tombstones the live row of +key+, tried under the client's agent first, then the
      # same user's row under any agent.
      def delete(key)
        out = call("memory.delete", { memory_key: key })
        DeleteResult.new(found: out["found"] == true, tombstoned: out["tombstoned"] == true, version: out["version"])
      end

      # Names the server and the identity, never the token.
      def inspect
        "#<#{self.class.name} #{@base_url} #{@identity.map { |k, v| "#{k}=#{v}" }.join(" ")}>"
      end

      private

      def request_headers(spec, idempotency_key)
        headers = { "Content-Type" => "application/json", "Accept" => "application/json",
                    "User-Agent" => SDK_IDENT, "X-Loom-SDK" => SDK_IDENT, "X-Request-Id" => SecureRandom.uuid }
        headers["Authorization"] = "Bearer #{@api_key}" if @api_key
        # The server keeps no replay of a read-only op: its resend just runs again.
        key = idempotency_key || (SecureRandom.uuid if spec&.deduplicated?)
        headers["Idempotency-Key"] = key.to_s if key
        headers
      end

      # One call: attempts until an answer to return or an error to raise. Every attempt
      # sends the same body and headers, so the same Idempotency-Key and X-Request-Id.
      def send_with_retries(op, spec, body, headers, policy)
        state = { op: op, attempt: 1, sent: 0, slept: 0.0, paced: 0.0, started: now,
                  request_id: headers["X-Request-Id"] }
        loop do
          headers["X-Loom-Retry-Count"] = state[:sent].to_s
          state[:sent] += 1
          response = attempt(op, spec, body, headers, policy, state)
          next if response.nil?
          return succeeded(op, response, state) if response.is_a?(Net::HTTPSuccess)

          # The server's echo of X-Request-Id, else the id this call sent (an error answer may not echo it).
          request_id = response["X-Request-Id"] || headers["X-Request-Id"]
          error = Error.from_response(op, response.code.to_i, response.body, request_id,
                                      response["Retry-After"], idempotent: spec&.idempotent == true)
          next if resend_after_answer?(response, error, spec, policy, state)

          raise error
        end
      rescue Error => e
        @log.error("#{op} failed after #{elapsed_ms(state)} ms: #{describe(e)} (rid=#{state[:request_id]})")
        raise
      end

      # The decoded answer of a 2xx, and its summary line.
      def succeeded(op, response, state)
        out = decode(op, response)
        rid = response["X-Request-Id"] || state[:request_id]
        # The last attempt's own time, as the TypeScript and Python SDKs report it: not
        # the backoff and 429 waits before it.
        @log.info("#{op} #{response.code} #{elapsed_ms(state, :attempt_started)} ms attempts=#{state[:sent]} rid=#{rid}")
        out
      end

      # The answer of one attempt, or nil after a connection failure the policy resends.
      def attempt(op, spec, body, headers, policy, state)
        state[:attempt_started] = now
        @log.debug("#{op} → POST #{@base_url}/v1/#{op} attempt #{state[:attempt]} waits #{(@read_timeout * 1000).round} " \
                   "ms bytes #{body.bytesize} #{Logging.redact_headers(headers)}")
        post(op, body, headers)
      rescue *NETWORK_ERRORS => e
        # A TLS failure (certificate verification stays on) does not pass by resending.
        raise Error.network(op, e) if e.is_a?(OpenSSL::SSL::SSLError)

        unsent = UNSENT_ERRORS.any? { |klass| e.is_a?(klass) }
        # A timeout or a cut-off answer may follow a write: resent only where the server dedups.
        resend = unsent ? policy.retry_connection_errors : may_rerun?(policy, spec)
        # The error raised in the end stays not retryable either way, as before: the call may have run.
        raise Error.network(op, e) unless resend && pause_before_next(policy, state, nil, e.message)

        nil
      end

      def resend_after_answer?(response, error, spec, policy, state)
        status = response.code.to_i
        return wait_out_rate_limit(response, state) if status == 429
        return false unless policy.statuses.include?(status) && !NEVER_RETRIED.key?(error.code)
        # Only the server's configuration class stops a resend; retryable false alone does
        # not (an INTERNAL_ERROR on an idempotent op is worth one).
        return false if error.detail["category"] == "configuration"
        return false unless spec&.idempotent || status == 408 || NEVER_RAN_CODES.include?(error.code)

        why = error.code.empty? ? status.to_s : "#{status} #{error.code}"
        pause_before_next(policy, state, RetryAfter.parse(response["Retry-After"] || error.detail["retry_after_s"]), why)
      end

      # A 429 is pacing, not a retry: wait the Retry-After (1 s when absent, zero, past or over
      # 60 s; at least 0.1 s) while the call's rate_limit_max_wait allows, and send again.
      def wait_out_rate_limit(response, state)
        wait = RetryAfter.for_rate_limit(response["Retry-After"])
        return false unless @rate_limit_max_wait.positive? && state[:paced] + wait <= @rate_limit_max_wait

        state[:paced] += wait
        @log.warn("#{state[:op]} 429 rate limited; waiting #{(wait * 1000).round} ms (rid=#{state[:request_id]})")
        @sleeper.call(wait)
        true
      end

      def may_rerun?(policy, spec)
        policy.retry_timeouts == :always || (policy.retry_timeouts == :idempotent && spec&.deduplicated?)
      end

      # Sleeps before the next attempt and counts it, or returns false when the policy says
      # stop: attempts spent, or the backoff budget. +hint+ is the server's wait in seconds;
      # +why+ names what is retried in the log line.
      def pause_before_next(policy, state, hint, why)
        return false if state[:attempt] >= policy.max_attempts

        delay = policy.backoff_delay(state[:attempt])
        delay = hint if policy.respect_retry_after && hint && hint <= policy.max_retry_after
        if delay > policy.total_backoff_budget - state[:slept]
          @log.warn("#{state[:op]}: backoff budget of #{(policy.total_backoff_budget * 1000).round} ms spent; giving up")
          return false
        end

        @log.warn("#{state[:op]} attempt #{state[:attempt]}/#{policy.max_attempts} #{why}; retrying in " \
                  "#{(delay * 1000).round} ms (rid=#{state[:request_id]})")
        @sleeper.call(delay)
        state[:slept] += delay
        state[:attempt] += 1
        true
      end

      # The decoded 2xx answer.
      def decode(op, response)
        parsed = parse_object(response.body)
        raise Error.invalid_response(op, response.code.to_i, response.body, response["X-Request-Id"]) if parsed.nil?

        parsed
      end

      def post(op, body, headers)
        uri = URI("#{@base_url}/v1/#{op}")
        request = Net::HTTP::Post.new(uri)
        headers.each { |name, value| request[name] = value }
        request.body = body
        Net::HTTP.start(uri.hostname, uri.port, use_ssl: uri.scheme == "https",
                                                open_timeout: @open_timeout, read_timeout: @read_timeout) do |http|
          http.request(request)
        end
      end

      def parse_object(text)
        parsed = JSON.parse(text.to_s)
        parsed.is_a?(Hash) ? parsed : nil
      rescue JSON::ParserError
        nil
      end

      # An error as a log line names it: its class, code and status, never its message.
      def describe(error)
        name = error.class.name.split("::").last
        name += " #{error.code}" unless error.code.to_s.empty?
        name += " (#{error.status})" unless error.status.to_i.zero?
        name
      end

      def now
        Process.clock_gettime(Process::CLOCK_MONOTONIC)
      end

      def elapsed_ms(state, from = :started)
        ((now - state[from]) * 1000).round
      end

      def first_non_blank(*values)
        values.map { |v| v.to_s.strip }.find { |v| !v.empty? }
      end
    end
  end
end
