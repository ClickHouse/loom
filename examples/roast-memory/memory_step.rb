# frozen_string_literal: true

require "clickhouse/loom"
require "digest"
require "json"
require "uri"

# Only this selected synthetic fact is eligible for saving.
module RoastMemory
  FACT = "Synthetic release rehearsal: run the smoke check before packaging."
  KEY = "release-rehearsal"
  IDENTITY = %w[org namespace agent user_id session_id].freeze
  class InvalidConfig < StandardError; end
  class InvalidReceipt < StandardError; end
  class StepFailed < StandardError; end

  def self.required(env, name)
    value = env.fetch(name, "")
    raise InvalidConfig unless value.is_a?(String) && !value.strip.empty? && !value.match?(/[\x00-\x20\x7f]/)

    value.dup.freeze
  end

  def self.run(action, env = ENV)
    raise InvalidConfig unless %w[save recall].include?(action)

    identity = IDENTITY.to_h { |field| [field.to_sym, required(env, "LOOM_#{field.upcase}")] }.freeze
    raise InvalidConfig if identity.values.any? { |value| %w[- *].include?(value) }

    url = required(env, "LOOM_BASE_URL")
    uri = URI.parse(url)
    loopback = %w[localhost 127.0.0.1 ::1].include?(uri.hostname)
    valid_transport = uri.scheme == "https" || (uri.scheme == "http" && loopback)
    raise InvalidConfig unless valid_transport && uri.host && !uri.userinfo && !uri.query && !uri.fragment
    raise InvalidConfig unless ["", "/"].include?(uri.path)

    token = required(env, "LOOM_API_KEY")
    client = ClickHouse::Loom::Client.new(
      base_url: url, api_key: token, **identity.slice(:org, :namespace, :agent),
      open_timeout: 5, read_timeout: 10, retry_policy: { max_attempts: 1 }, rate_limit_max_wait: 0
    )
    # Length-delimited JSON avoids ambiguous concatenation; this is not authorization.
    digest = Digest::SHA256.hexdigest(JSON.generate(identity.values + [KEY]))
    fields = identity.merge(memory_key: "roast-example/#{digest}")
    action == "save" ? save(client, fields) : recall(client, fields)
  rescue InvalidConfig, URI::InvalidURIError
    { "status" => "error", "reason" => "invalid_config" }
  rescue ClickHouse::Loom::Error
    # A failed response cannot tell us whether a write reached storage.
    { "status" => "error", "reason" => "backend_error" }
  rescue InvalidReceipt
    { "status" => "error", "reason" => "invalid_receipt" }
  end

  def self.save(client, fields)
    answer = client.memory.set(**fields, value: FACT, memory_type: "semantic", source_class: "derived")
    valid = answer["status"] == "active" && answer["memory_id"].is_a?(String) && !answer["memory_id"].empty?
    valid &&= answer["version"].is_a?(Integer) && answer["version"].positive?
    valid &&= [true, false].include?(answer["noop"])
    raise InvalidReceipt unless valid

    { "status" => "saved", "noop" => answer["noop"] }
  end

  def self.recall(client, fields)
    answer = client.memory.get(**fields)
    return { "status" => "abstain" } if answer["found"] == false
    raise InvalidReceipt unless answer["found"] == true && answer["value"].is_a?(String)

    { "status" => "found", "value" => answer["value"] }
  end
end
