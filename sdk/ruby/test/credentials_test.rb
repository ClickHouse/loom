# frozen_string_literal: true

require_relative "test_helper"
require "fileutils"
require "logger"
require "stringio"

# Where the bearer token comes from: a token provider (asked on every request, and once
# more after a 401) and ~/.loom/token, as in the TypeScript and Python SDKs.
class CredentialsTest < LoomTest
  UNAUTHORIZED = { detail: { code: "AUTH_INVALID", message: "token expired" } }.freeze

  # A provider that hands out tokens in turn and records what it was asked.
  class Rotating
    attr_reader :asked

    def initialize(*tokens)
      @tokens = tokens
      @asked = []
    end

    def token(refused)
      @asked << refused
      @tokens.length > 1 ? @tokens.shift : @tokens.first
    end
  end

  def auth(request)
    request.headers["authorization"]
  end

  def write_token_file(text)
    dir = File.join(Dir.home, ".loom")
    FileUtils.mkdir_p(dir)
    File.write(File.join(dir, "token"), text)
  end

  def test_the_token_file_is_the_last_credential
    write_token_file("file-token
second line
")
    respond_json(200, { found: false })
    client.get("k")
    assert_equal "Bearer file-token", auth(sent)
  end

  def test_a_blank_or_missing_token_file_sends_no_authorization
    respond_json(200, { found: false })
    client.get("k")
    assert_nil auth(sent)

    write_token_file("  \n")
    respond_json(200, { found: false })
    client.get("k")
    assert_nil auth(sent)
  end

  def test_loom_api_key_wins_over_the_token_file
    write_token_file("file-token\n")
    ENV["LOOM_API_KEY"] = "env-token"
    respond_json(200, { found: false })
    client.get("k")
    assert_equal "Bearer env-token", auth(sent)
  end

  def test_a_provider_token_goes_on_every_request
    provider = Rotating.new("t1", "t2")
    respond_json(200, { found: false })
    respond_json(200, { found: false })
    c = client(token_provider: provider)
    c.get("a")
    assert_equal "Bearer t1", auth(sent)
    c.get("b")
    assert_equal "Bearer t2", auth(sent)
    assert_equal [nil, nil], provider.asked
  end

  def test_a_provider_wins_over_loom_api_key_and_the_token_file
    write_token_file("file-token\n")
    ENV["LOOM_API_KEY"] = "env-token"
    respond_json(200, { found: false })
    client(token_provider: Rotating.new("provided")).get("k")
    assert_equal "Bearer provided", auth(sent)
  end

  def test_a_401_is_sent_once_more_with_a_fresh_token
    provider = Rotating.new("stale", "fresh")
    respond_json(401, UNAUTHORIZED)
    respond_json(200, { found: true, value: "v" })
    out = client(token_provider: provider).get("k")

    assert_equal "v", out.value
    assert_equal ["Bearer stale", "Bearer fresh"], @server.requests.map { |r| auth(r) }
    assert_equal [nil, "stale"], provider.asked
    # The same call: one request id, and the resend counted.
    assert_equal 1, @server.requests.map { |r| r.headers["x-request-id"] }.uniq.length
    assert_equal %w[0 1], @server.requests.map { |r| r.headers["x-loom-retry-count"] }
  end

  def test_a_provider_with_no_fresh_token_leaves_the_401_standing
    provider = Rotating.new("only")
    respond_json(401, UNAUTHORIZED)
    error = assert_raises(Loom::AuthInvalid) { client(token_provider: provider).get("k") }

    assert_equal 401, error.status
    assert_equal 1, @server.requests.length
    assert_equal [nil, "only"], provider.asked
  end

  def test_a_second_401_is_raised
    respond_json(401, UNAUTHORIZED)
    respond_json(401, UNAUTHORIZED)
    respond_json(200, { found: false })
    assert_raises(Loom::AuthInvalid) { client(token_provider: Rotating.new("a", "b", "c")).get("k") }
    assert_equal 2, @server.requests.length
  end

  def test_the_401_resend_spends_no_retry_attempt
    respond_json(401, UNAUTHORIZED)
    respond_json(503, { detail: { code: "BACKEND_UNAVAILABLE", message: "busy" } })
    respond_json(200, { found: false })
    c = client(token_provider: Rotating.new("a", "b"), retry_policy: { max_attempts: 2, backoff: 0.0, backoff_jitter: 0.0 },
               sleeper: ->(_) {})
    c.get("k")
    assert_equal 3, @server.requests.length
  end

  def test_a_401_with_a_static_key_is_not_resent
    respond_json(401, UNAUTHORIZED)
    respond_json(200, { found: false })
    assert_raises(Loom::AuthInvalid) { client(api_key: "static").get("k") }
    assert_equal 1, @server.requests.length
  end

  def test_an_empty_provider_token_sends_no_authorization
    respond_json(200, { found: false })
    client(token_provider: ->(_refused) { "" }).get("k")
    assert_nil auth(sent)
  end

  def test_a_proc_provider_is_called_with_the_refused_token
    asked = []
    provider = lambda do |refused|
      asked << refused
      refused ? "fresh" : "stale"
    end
    respond_json(401, UNAUTHORIZED)
    respond_json(200, { found: false })
    client(token_provider: provider).get("k")
    assert_equal [nil, "stale"], asked
    assert_equal "Bearer fresh", auth(sent)
  end

  def test_a_zero_argument_proc_works
    respond_json(200, { found: false })
    client(token_provider: -> { "plain" }).get("k")
    assert_equal "Bearer plain", auth(sent)
  end

  def test_api_key_and_token_provider_together_are_refused
    error = assert_raises(ArgumentError) { client(api_key: "k", token_provider: Rotating.new("t")) }
    assert_match(/not both/, error.message)
  end

  def test_a_provider_bound_to_another_origin_is_refused
    provider = Struct.new(:origin, :label) { def token(_) = "t" }.new("https://loom.example", "login:default")
    error = assert_raises(ArgumentError) { client(token_provider: provider) }
    assert_match(/refusing to send login:default's token to 127\.0\.0\.1/, error.message)
    assert_empty @server.requests
  end

  def test_a_provider_bound_to_this_origin_is_used
    provider = Struct.new(:origin) { def token(_) = "t" }.new(@server.base_url)
    respond_json(200, { found: false })
    client(token_provider: provider).get("k")
    assert_equal "Bearer t", auth(sent)
  end

  def test_the_configured_provider_is_used_unless_an_api_key_is_passed
    Loom.configure { |c| c.token_provider = Rotating.new("configured") }
    respond_json(200, { found: false })
    respond_json(200, { found: false })
    client.get("a")
    assert_equal "Bearer configured", auth(sent)
    client(api_key: "argument").get("b")
    assert_equal "Bearer argument", auth(sent)
  end

  def test_the_token_is_not_logged_or_inspected
    log = StringIO.new
    respond_json(401, UNAUTHORIZED)
    respond_json(200, { found: false })
    c = client(token_provider: Rotating.new("secret-stale-token", "secret-fresh-token"),
               logger: Logger.new(log), log_level: :debug)
    c.get("k")
    refute_includes log.string, "secret-stale-token"
    refute_includes log.string, "secret-fresh-token"
    assert_includes log.string, "401; retrying once with a fresh token"
    refute_includes c.inspect, "secret"
  end
end
