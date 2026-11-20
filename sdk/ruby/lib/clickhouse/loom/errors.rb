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

    # A non-2xx answer from the server, or a call that got no usable answer.
    # Match on +code+, never on the message: the code is the contract.
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

      alias retryable? retryable

      def initialize(message, op:, status: 0, code: "", category: "input", retryable: false,
                     retry_after: nil, request_id: "")
        super(message)
        @op = op
        @status = status
        @code = code
        @category = category
        @retryable = retryable
        @retry_after = retry_after
        @request_id = request_id
      end

      def to_s
        text = super
        return text if status.zero?

        code.empty? ? "#{op}: HTTP #{status}: #{text}" : "#{op}: HTTP #{status} #{code}: #{text}"
      end

      # The Error for a non-2xx answer.
      def self.from_response(op, status, body, request_id, retry_after_header)
        detail = extract_detail(body)
        category, retryable = class_by_status(status)
        category = detail["category"] if CATEGORIES.include?(detail["category"])
        retryable = detail["retryable"] if [true, false].include?(detail["retryable"])
        message = detail["message"].to_s
        message = "HTTP #{status}" if message.empty?
        new(message, op: op, status: status, code: detail["code"].to_s, category: category,
                     retryable: retryable, retry_after: seconds(detail["retry_after_s"] || retry_after_header),
                     request_id: request_id.to_s)
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

      # The server's own rule for an error whose body names no category, such as a
      # proxy's page or an older server.
      def self.class_by_status(status)
        case status
        when 401, 403 then ["auth", false]
        when 408, 429 then ["capacity", true]
        when 502, 503, 504 then ["dependency", true]
        when 500.. then ["internal", false]
        else ["input", false]
        end
      end
      private_class_method :class_by_status

      def self.seconds(value)
        wait = Float(value)
        wait.positive? ? wait : nil
      rescue ArgumentError, TypeError
        nil
      end
      private_class_method :seconds

      # At most +limit+ bytes of +text+, cut on a character boundary.
      def self.snippet(text, limit)
        text.to_s.dup.force_encoding(Encoding::UTF_8).byteslice(0, limit).scrub("").strip
      end
    end
  end
end
