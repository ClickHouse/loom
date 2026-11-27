# frozen_string_literal: true

module ClickHouse
  module Loom
    # Logging with one redaction point, the same lines and rules as the TypeScript
    # (logging.ts) and Python (_logging.py) SDKs.
    #
    # Logging is opt-in: a client logs only when it is given a logger (Client.new(logger:)
    # or ClickHouse::Loom.configure), any object with debug, info, warn and error, such as
    # a ::Logger or Rails.logger. It then writes one line per completed call at info
    # ("memory.get 200 12 ms attempts=1 rid=..."), each retry and 429 wait at warn, the
    # final failure at error, and the request with its headers at debug. Lines below the
    # level (log_level:, else LOOM_LOG_LEVEL, else warn) are dropped before they reach the
    # logger.
    #
    # Credentials never reach the logger: header values go through redact_headers, and
    # every line goes through redact_text, so a bearer token that ends up in a message by
    # mistake still shows only its last four characters, and a URL loses its user
    # information (user:password@).
    module Logging
      LEVELS = %i[debug info warn error off].freeze
      DEFAULT_LEVEL = :warn

      RANK = { debug: 0, info: 1, warn: 2, error: 3, off: 4 }.freeze

      # LOOM_LOG_LEVEL is shared with the server and the Python SDK, which take Python's
      # level names.
      STDLIB_ALIASES = { "warning" => :warn, "critical" => :error, "fatal" => :error, "notset" => :debug }.freeze

      # Credential headers: only the last four characters survive, and for the
      # authorization headers their scheme ("Bearer"). A cookie or an API key has no
      # scheme, so its first word is part of the secret.
      CREDENTIAL_HEADERS = %w[authorization proxy-authorization x-api-key cookie set-cookie].freeze
      SCHEME_HEADERS = %w[authorization proxy-authorization].freeze

      # The user information in a URL: "https://user:secret@host" keeps "https://host".
      URL_USERINFO = %r{\b([a-z][a-z0-9+.-]*://)[^\s/?#@]+@}i

      BEARER = /\b(Bearer|Basic|Token)\s+([A-Za-z0-9._~+\/=-]{8,})/i

      module_function

      # A level from a String or Symbol, naming +source+ when it is not one. The value
      # itself is never repeated: a credential set there by mistake would reach the log.
      def parse_level(value, source)
        key = value.to_s.strip.downcase
        return key.to_sym if LEVELS.include?(key.to_sym)
        return STDLIB_ALIASES[key] if STDLIB_ALIASES.key?(key)

        raise ArgumentError, "#{source}: invalid log level; expected one of #{LEVELS.join(", ")}"
      end

      # A secret as a log line may show it: "…" and, past 8 characters, its last four.
      def redact_secret(secret)
        secret = secret.to_s.strip
        secret.length > 8 ? "…#{secret[-4..]}" : "…"
      end

      # The value of header +name+ as it may appear in a log line.
      def redact_header_value(name, value)
        name = name.to_s.downcase
        return value unless CREDENTIAL_HEADERS.include?(name)
        return redact_secret(value) unless SCHEME_HEADERS.include?(name)

        scheme, secret = value.to_s.strip.split(/\s+/, 2)
        secret ? "#{scheme} #{redact_secret(secret)}" : redact_secret(scheme)
      end

      # A copy of +headers+ safe for a log line. The one redaction point.
      def redact_headers(headers)
        headers.to_h { |name, value| [name, redact_header_value(name, value)] }
      end

      # Rewrites bearer / basic / token credentials embedded in free text, and drops the
      # user information from any URL in it.
      def redact_text(text)
        text.to_s
            .gsub(BEARER) { "#{Regexp.last_match(1)} #{redact_secret(Regexp.last_match(2))}" }
            .gsub(URL_USERINFO, '\1')
      end

      # The logger a client writes to: +sink+ filtered to +level+ and above, every line
      # redacted. A nil +sink+ (no logger configured) or level :off logs nothing.
      # +level+ nil reads LOOM_LOG_LEVEL; a value there it cannot read is a warning on
      # +sink+, not a failed client, and the default level applies.
      def logger_for(sink, level, env: ENV)
        return NULL if sink.nil?

        level = level.nil? ? level_from_env(sink, env) : parse_level(level, "log_level")
        level == :off ? NULL : Leveled.new(sink, level)
      end

      def level_from_env(sink, env)
        raw = env.fetch("LOOM_LOG_LEVEL", "").to_s.strip
        return DEFAULT_LEVEL if raw.empty?

        parse_level(raw, "LOOM_LOG_LEVEL")
      rescue ArgumentError => e
        sink.warn(redact_text("#{e.message}; using #{DEFAULT_LEVEL}"))
        DEFAULT_LEVEL
      end

      # +sink+ filtered to +level+ and above, with every line redacted.
      class Leveled
        attr_reader :level

        def initialize(sink, level)
          @sink = sink
          @level = level
        end

        %i[debug info warn error].each do |at|
          define_method(at) do |message|
            @sink.public_send(at, Logging.redact_text(message)) if RANK[at] >= RANK[@level]
            nil
          end
        end
      end

      # Logs nothing.
      class Null
        def debug(_message) = nil
        def info(_message) = nil
        def warn(_message) = nil
        def error(_message) = nil
      end

      NULL = Null.new
    end
  end
end
