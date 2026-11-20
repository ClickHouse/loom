# frozen_string_literal: true

require "json"
require "net/http"
require "uri"

require_relative "errors"
require_relative "version"

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

    # Calls Loom's memory operations as one org, namespace and agent.
    #
    # Every operation is POST /v1/<op> with a JSON body. The org, namespace and agent
    # are bound once and sent with every call; a blank one is left out of the body, so
    # the server's default applies. The client sends one request per call and never
    # retries. It does not follow redirects, so a memory body and token never go to
    # another origin: a 3xx answer raises an Error.
    class Client
      # base_url: defaults to LOOM_BASE_URL, then DEFAULT_BASE_URL. api_key: defaults to
      # LOOM_API_KEY; with neither, no Authorization header is sent, which a server with
      # auth off accepts.
      def initialize(base_url: nil, api_key: nil, org: nil, namespace: nil, agent: nil,
                     open_timeout: 10, read_timeout: 60)
        @base_url = first_non_blank(base_url, ENV.fetch("LOOM_BASE_URL", nil), DEFAULT_BASE_URL).sub(%r{/+\z}, "")
        # An exported but empty LOOM_API_KEY must not send "Bearer " and turn a clear 401 into a puzzle.
        @api_key = first_non_blank(api_key, ENV.fetch("LOOM_API_KEY", nil))
        @identity = { org: org, namespace: namespace, agent: agent }.reject { |_, v| blank?(v) }
        @open_timeout = open_timeout
        @read_timeout = read_timeout
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
      # the server use 5; +tags+ keeps only rows carrying any of them.
      def search(query, top_k: nil, memory_type: nil, tags: nil)
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

      private

      # Posts +input+ to /v1/<op> and returns the decoded 2xx answer.
      def call(op, input)
        response = post(op, JSON.generate(@identity.merge(input)))
        request_id = response["X-Request-Id"].to_s
        unless response.is_a?(Net::HTTPSuccess)
          raise Error.from_response(op, response.code.to_i, response.body, request_id, response["Retry-After"])
        end

        parsed = parse_object(response.body)
        raise Error.invalid_response(op, response.code.to_i, response.body, request_id) if parsed.nil?

        parsed
      end

      def post(op, body)
        uri = URI("#{@base_url}/v1/#{op}")
        request = Net::HTTP::Post.new(uri)
        request["Content-Type"] = "application/json"
        request["Accept"] = "application/json"
        request["User-Agent"] = SDK_IDENT
        request["X-Loom-SDK"] = SDK_IDENT
        request["Authorization"] = "Bearer #{@api_key}" if @api_key
        request.body = body
        Net::HTTP.start(uri.hostname, uri.port, use_ssl: uri.scheme == "https",
                                                open_timeout: @open_timeout, read_timeout: @read_timeout) do |http|
          http.request(request)
        end
      rescue SystemCallError, SocketError, IOError, Timeout::Error, Net::ProtocolError => e
        # No complete answer: the call may have run, so it is not marked retryable.
        raise Error.network(op, e)
      end

      def parse_object(text)
        parsed = JSON.parse(text.to_s)
        parsed.is_a?(Hash) ? parsed : nil
      rescue JSON::ParserError
        nil
      end

      def first_non_blank(*values)
        values.map { |v| v.to_s.strip }.find { |v| !v.empty? }
      end

      def blank?(value)
        value.to_s.strip.empty?
      end
    end
  end
end
