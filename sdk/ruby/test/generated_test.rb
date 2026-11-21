# frozen_string_literal: true

require_relative "test_helper"

class GeneratedTest < LoomTest
  UUID = /\A\h{8}-\h{4}-\h{4}-\h{4}-\h{12}\z/

  def test_a_generated_method_posts_its_op_with_the_bound_identity_and_the_fields
    respond_json(200, { memory_id: "m1", version: 1 })

    out = client.memory.set(memory_key: "deploy_window", value: "Tue and Thu", tags: %w[ops])

    assert_equal "/v1/memory.set", sent.path
    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant", "memory_key" => "deploy_window",
                   "value" => "Tue and Thu", "tags" => %w[ops] }, sent_body)
    assert_equal({ "memory_id" => "m1", "version" => 1 }, out)
  end

  def test_an_op_the_server_dedups_gets_an_idempotency_key_and_a_read_only_op_none
    respond_json(200, {})
    respond_json(200, { found: false })

    client.memory.set(memory_key: "k", value: "v")
    set_key = @server.requests[0].headers["idempotency-key"]
    client.memory.get(memory_key: "k")

    assert_match UUID, set_key
    refute @server.requests[1].headers.key?("idempotency-key")
  end

  def test_a_keyword_overrides_the_bound_identity_for_one_call
    respond_json(200, {})
    respond_json(200, {})

    c = client
    c.memory.get(memory_key: "k", org: "acme", namespace: "user-42", agent: "support-bot")
    c.memory.get(memory_key: "k")

    assert_equal({ "org" => "acme", "namespace" => "user-42", "agent" => "support-bot", "memory_key" => "k" },
                 JSON.parse(@server.requests[0].body))
    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant", "memory_key" => "k" },
                 JSON.parse(@server.requests[1].body))
  end

  def test_a_nil_keyword_is_left_out_and_a_body_hash_can_send_an_explicit_null
    respond_json(200, {})

    client.memory.set({ "ttl_seconds" => nil, "value" => "from body" }, memory_key: "k", value: "v", tags: nil)

    assert_equal({ "org" => "dev", "namespace" => "local", "agent" => "assistant", "memory_key" => "k",
                   "value" => "v", "ttl_seconds" => nil }, sent_body)
  end

  def test_a_keyword_the_op_does_not_take_is_refused_before_sending
    error = assert_raises(ArgumentError) { client.memory.set(memory_key: "k", valeu: "v") }

    assert_includes error.message, "memory.set has no field valeu (did you mean value?)"
    assert_empty @server.requests
  end

  def test_request_options_carry_the_idempotency_key_and_refuse_unknown_ones
    respond_json(200, {})

    client.memory.set(memory_key: "k", value: "v", request_options: { idempotency_key: "job-7" })

    assert_equal "job-7", sent.headers["idempotency-key"]
    assert_raises(ArgumentError) { client.memory.get(memory_key: "k", request_options: { timeout: 1 }) }
  end

  def test_a_field_named_like_an_option_stays_in_the_body
    respond_json(200, {})

    client.task.create(title: "t", idempotency_key: "task-key-1")

    assert_equal "task-key-1", sent_body["idempotency_key"]
    refute_equal "task-key-1", sent.headers["idempotency-key"]
  end

  def test_op_names_with_more_segments_and_shadowing_names_map_like_the_python_sdk
    respond_json(200, {})
    respond_json(200, {})

    client.tracker.source_add(name: "gh")
    client.memory.inspect_(subject: "memory", id: "m1")

    assert_equal %w[/v1/tracker.source.add /v1/memory.inspect], @server.requests.map(&:path)
    assert_equal "#<ClickHouse::Loom::Generated::MemoryOps>", client.memory.inspect
  end

  def test_search_with_no_argument_is_the_search_group
    respond_json(200, { results: [] })

    client.search.query(query: "deploys")

    assert_instance_of Loom::Generated::SearchOps, client.search
    assert_equal "/v1/search.query", sent.path
  end

  def test_an_op_the_table_does_not_know_is_sent_as_is_with_no_idempotency_key
    respond_json(200, { ok: true })

    out = client.call("future.op", { "x" => 1 })

    assert_equal "/v1/future.op", sent.path
    assert_equal 1, sent_body["x"]
    refute sent.headers.key?("idempotency-key")
    assert_equal({ "ok" => true }, out)
  end

  def test_every_op_in_the_table_has_a_method_on_its_group_and_the_client_reaches_every_group
    ops = Loom::Generated::OPS
    c = client

    assert_equal Loom::Generated::OP_COUNT, ops.size
    ops.each_value do |spec|
      group = c.op_group(spec.group)

      assert_respond_to group, spec.method_name, spec.name
      assert_same group, c.public_send(spec.group) unless spec.group == "search"
    end
    assert_equal ops.values.map(&:group).uniq.sort, Loom::Generated::GROUPS.keys
  end

  def test_no_generated_name_shadows_a_method_every_object_has
    taken = Object.public_instance_methods.map(&:to_s)
    methods = Loom::Generated::OPS.values.map(&:method_name)

    assert_empty methods & taken
    assert_empty Loom::Generated::GROUPS.keys & taken
  end

  def test_the_spec_names_the_idempotency_and_the_fields_of_an_op
    spec = Loom::Generated::OPS.fetch("memory.set")

    assert spec.deduplicated?
    assert_includes spec.fields, "memory_key"
    assert_includes spec.required, "value"
    refute Loom::Generated::OPS.fetch("memory.get").deduplicated?
  end
end
