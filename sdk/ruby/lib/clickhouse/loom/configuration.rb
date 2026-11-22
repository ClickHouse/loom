# frozen_string_literal: true

module ClickHouse
  module Loom
    # The process-wide defaults a Client.new without arguments takes, set once with
    # ClickHouse::Loom.configure (in Rails, an initializer). An argument to Client.new
    # wins over a value set here, and a value set here over LOOM_BASE_URL / LOOM_API_KEY.
    class Configuration
      # The server and the token; nil falls back to LOOM_BASE_URL / LOOM_API_KEY.
      attr_accessor :base_url, :api_key
      # The identity every call is sent as; a call can override each one.
      attr_accessor :org, :namespace, :agent
      # Seconds to wait for the connection and for each answer.
      attr_accessor :open_timeout, :read_timeout
      # A Hash of RetryPolicy fields set over RetryPolicy.default, like { max_attempts: 5 }.
      attr_accessor :retry_policy
      # Seconds one call may spend waiting out 429 answers; 0 raises the first one.
      attr_accessor :rate_limit_max_wait

      def initialize
        @open_timeout = 10
        @read_timeout = 60
        @rate_limit_max_wait = 15
      end
    end

    class << self
      # The configuration Client.new reads its defaults from.
      def configuration
        @configuration ||= Configuration.new
      end

      #   ClickHouse::Loom.configure do |c|
      #     c.base_url = "https://loom.example.com"
      #     c.api_key = Rails.application.credentials.loom_api_key
      #     c.org = "acme"
      #     c.namespace = "support"
      #   end
      def configure
        yield configuration
        configuration
      end

      # Forgets what configure set (for tests).
      def reset_configuration!
        @configuration = Configuration.new
      end
    end
  end
end
