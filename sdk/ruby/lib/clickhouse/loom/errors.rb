# frozen_string_literal: true

require "json"

module ClickHouse
  module Loom
    # The categories the server names in detail.category: whose move an error is.
    CATEGORIES = %w[input auth capacity dependency configuration internal].freeze

    # The code of an Error for a 2xx answer whose body is not a JSON object.
    CODE_INVALID_RESPONSE = "INVALID_RESPONSE"

    # The code of an Error for a call that got no complete answer: the call may have run.
    CODE_NETWORK = "NETWORK"

    # 5xx codes never resent, with their category (the TypeScript SDK's NEVER_RETRIED):
    # a deployment's configuration, a model call that names no customer, and a write
    # already committed whose rows are still on their way.
    NEVER_RETRIED = {
      "ANALYSIS_UNAVAILABLE" => "configuration",
      "ASSISTANT_UNAVAILABLE" => "configuration",
      "CODED_WIDTH_MISMATCH" => "configuration",
      "GATEWAY_REQUIRED" => "configuration",
      "MODEL_CALL_UNATTRIBUTED" => "internal",
      "PUBLICATION_PENDING" => "dependency",
      "TENANCY_REFUSED" => "configuration"
    }.freeze

    # Codes the server answers before running the handler: resending runs the op once.
    NEVER_RAN_CODES = %w[AUTH_UNAVAILABLE].freeze

    # A non-2xx answer from the server, or a call that got no usable answer.
    # Match on +code+, never on the message: the code is the contract. An answer with a
    # server code raises that code's subclass (PinnedConflict, RateLimited, ...: see
    # generated/errors.rb), so a rescue can name the class instead.
    class Error < StandardError
      # The operation called, like "memory.get".
      attr_reader :op
      # The HTTP status; 0 when no answer came.
      attr_reader :status
      # The server's detail.code, like "RATE_LIMITED"; "" when it named none.
      attr_reader :code
      # One of CATEGORIES, from the server's detail, else from the HTTP status.
      attr_reader :category
      # Whether resending the same request can succeed later.
      attr_reader :retryable
      # Seconds the server asked to wait before resending; nil when it named no wait.
      attr_reader :retry_after
      # The server's X-Request-Id; quote it in a bug report.
      attr_reader :request_id
      # The server's whole detail object (like detail["pinned_blocker_id"]); {} when none.
      attr_reader :detail

      alias retryable? retryable

      def initialize(message, op:, status: 0, code: "", category: "input", retryable: false,
                     retry_after: nil, request_id: "", detail: {})
        super(message)
        @op = op
        @status = status
        @code = code
        @category = category
        @retryable = retryable
        @retry_after = retry_after
        @request_id = request_id
        @detail = detail
      end

      def to_s
        text = super
        return text if status.zero?

        code.empty? ? "#{op}: HTTP #{status}: #{text}" : "#{op}: HTTP #{status} #{code}: #{text}"
      end

      # The Error for a non-2xx answer: the subclass of its code, else Error. +idempotent+
      # false (an op the registry does not mark idempotent) makes a 5xx whose body names no
      # +retryable+ not retryable, since the handler may have written before it failed.
      def self.from_response(op, status, body, request_id, retry_after_header, idempotent: true)
        detail = extract_detail(body)
        code = detail["code"].to_s
        category, retryable = default_class(code, status)
        category = detail["category"] if CATEGORIES.include?(detail["category"])
        named = [true, false].include?(detail["retryable"])
        retryable = detail["retryable"] if named
        retryable = false if !idempotent && !named && status >= 500 && !NEVER_RAN_CODES.include?(code)
        message = detail["message"].to_s
        message = "HTTP #{status}" if message.empty?
        klass = ERROR_CLASSES.fetch(code, Error)
        klass.new(message, op: op, status: status, code: code, category: category, retryable: retryable,
                           retry_after: seconds(detail["retry_after_s"] || retry_after_header),
                           request_id: request_id.to_s, detail: detail)
      end

      # The Error for a call that never got an answer to read.
      def self.network(op, cause)
        new("#{cause.class}: #{cause.message}", op: op, code: CODE_NETWORK, category: "dependency")
      end

      # The Error for a 2xx answer that is not a JSON object. Most often a proxy's page.
      def self.invalid_response(op, status, body, request_id)
        new(snippet(body, 200), op: op, status: status, code: CODE_INVALID_RESPONSE,
                                category: "internal", request_id: request_id.to_s)
      end

      def self.extract_detail(body)
        parsed = begin
          JSON.parse(body.to_s)
        rescue JSON::ParserError
          nil
        end
        return { "message" => snippet(body, 500) } unless parsed.is_a?(Hash)

        case (detail = parsed.fetch("detail", parsed))
        when Hash then detail
        when String then { "message" => detail }
        else {}
        end
      end
      private_class_method :extract_detail

      # The class of an error whose body names none, such as a proxy's page or an older
      # server: a code never resent, else the server's own rule by HTTP status.
      def self.default_class(code, status)
        return [NEVER_RETRIED[code], false] if NEVER_RETRIED.key?(code)

        case status
        when 401, 403 then ["auth", false]
        when 408, 429 then ["capacity", true]
        when 502, 503, 504 then ["dependency", true]
        when 500.. then ["internal", false]
        else ["input", false]
        end
      end
      private_class_method :default_class

      # A Retry-After header (seconds or an HTTP-date) or detail.retry_after_s, in
      # seconds; nil when absent, unreadable or not positive.
      def self.seconds(value)
        wait = RetryAfter.parse(value)
        wait&.positive? ? wait : nil
      end
      private_class_method :seconds

      # At most +limit+ bytes of +text+, cut on a character boundary.
      def self.snippet(text, limit)
        text.to_s.dup.force_encoding(Encoding::UTF_8).byteslice(0, limit).scrub("").strip
      end
    end
  end
end
