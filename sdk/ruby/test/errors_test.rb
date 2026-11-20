# frozen_string_literal: true

require_relative "test_helper"

class ErrorsTest < LoomTest
  # status, code, category, retryable: one answer per category the server names.
  CATEGORY_ANSWERS = [
    [422, "VALIDATION_FAILED", "input", false],
    [403, "AUTH_INSUFFICIENT", "auth", false],
    [429, "RATE_LIMITED", "capacity", true],
    [503, "STORE_UNAVAILABLE", "dependency", true],
    [503, "TENANCY_REFUSED", "configuration", false],
    [500, "INTERNAL_ERROR", "internal", false]
  ].freeze

  CATEGORY_ANSWERS.each do |status, code, category, retryable|
    define_method("test_a_#{category}_answer_carries_its_code_category_retryable_and_status") do
      respond_json(status, { detail: { code: code, message: "refused", category: category, retryable: retryable } },
                   { "X-Request-Id" => "req-1" })

      error = assert_raises(Loom::Error) { client.get("k") }

      assert_equal code, error.code
      assert_equal category, error.category
      assert_equal retryable, error.retryable?
      assert_equal status, error.status
      assert_equal "memory.get", error.op
      assert_equal "req-1", error.request_id
      assert_equal "memory.get: HTTP #{status} #{code}: refused", error.message
    end
  end

  def test_the_server_body_wins_over_the_class_of_the_http_status
    respond_json(503, { detail: { code: "TENANCY_REFUSED", message: "m", category: "configuration",
                                  retryable: false } })

    error = assert_raises(Loom::Error) { client.set("k", "v") }

    assert_equal "configuration", error.category
    refute error.retryable?
  end

  def test_a_rate_limit_names_how_long_to_wait_from_the_body_or_the_header
    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow down", retry_after_s: 2.5 } })
    from_body = assert_raises(Loom::Error) { client.get("k") }

    respond_json(429, { detail: { code: "RATE_LIMITED", message: "slow down" } }, { "Retry-After" => "7" })
    from_header = assert_raises(Loom::Error) { client.get("k") }

    assert_in_delta 2.5, from_body.retry_after
    assert_in_delta 7.0, from_header.retry_after
    assert_equal "capacity", from_header.category
    assert from_header.retryable?
  end

  def test_an_answer_with_no_wait_has_no_retry_after
    respond_json(404, { detail: { code: "RESOURCE_NOT_FOUND", message: "gone" } })

    assert_nil assert_raises(Loom::Error) { client.get("k") }.retry_after
  end

  def test_a_plain_string_detail_becomes_the_message
    respond_json(400, { detail: "bad request" })

    error = assert_raises(Loom::Error) { client.get("k") }

    assert_equal "", error.code
    assert_equal "memory.get: HTTP 400: bad request", error.message
  end

  def test_a_proxy_page_is_classed_by_its_http_status
    @server.respond(502, "<html>Bad Gateway</html>")

    error = assert_raises(Loom::Error) { client.get("k") }

    assert_equal 502, error.status
    assert_equal "dependency", error.category
    assert error.retryable?
    assert_includes error.message, "Bad Gateway"
  end

  def test_a_2xx_answer_that_is_not_json_is_an_invalid_response_that_is_not_retryable
    @server.respond(200, "<html>captive portal</html>")

    error = assert_raises(Loom::Error) { client.get("k") }

    assert_equal Loom::CODE_INVALID_RESPONSE, error.code
    assert_equal "internal", error.category
    refute error.retryable?
    assert_equal 200, error.status
  end

  def test_a_redirect_is_an_error_and_is_not_followed
    @server.respond(302, "", { "Location" => "http://127.0.0.1:1/elsewhere" })

    error = assert_raises(Loom::Error) { client(api_key: "tok").set("k", "secret") }

    assert_equal 302, error.status
    assert_equal 1, @server.requests.size
  end

  def test_a_connection_that_never_answers_is_a_network_error_that_is_not_retryable
    error = assert_raises(Loom::Error) do
      Loom::Client.new(base_url: "http://127.0.0.1:1", open_timeout: 2).get("k")
    end

    assert_equal Loom::CODE_NETWORK, error.code
    assert_equal "dependency", error.category
    refute error.retryable?
    assert_equal 0, error.status
  end
end
