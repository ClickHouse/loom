# frozen_string_literal: true

module ClickHouse
  module Loom
    # The declared spec of one served op (Generated::OPS holds one per op). +fields+
    # are the names its input accepts, +required+ the ones it cannot do without (the
    # org, namespace and agent among them come from the client's identity).
    OpSpec = Struct.new(:name, :group, :method_name, :idempotent, :read_only, :role, :maturity,
                        :fields, :required) do
      # Whether a call gets an Idempotency-Key the server deduplicates on.
      def deduplicated?
        idempotent && !read_only
      end
    end

    # The ops of one group, as methods: client.memory is a MemoryOps. Each generated
    # method takes the op's fields as keywords, an optional body Hash under them, and
    # request_options: { idempotency_key:, retry_policy: }.
    class OpGroup
      REQUEST_OPTIONS = %i[idempotency_key retry_policy].freeze

      def initialize(client)
        @client = client
      end

      def inspect
        "#<#{self.class.name}>"
      end

      private

      # A nil keyword is left out of the body, so the server's default applies; send an
      # explicit null in +body+. A keyword the op does not accept is an ArgumentError
      # before anything is sent; a +body+ key is sent as it is.
      def call_op(op, body, fields, request_options)
        spec = Generated::OPS.fetch(op)
        unknown = fields.keys.map(&:to_s) - spec.fields
        raise ArgumentError, unknown_fields_message(spec, unknown) unless unknown.empty?
        raise ArgumentError, "#{op}: body must be a Hash, got #{body.class}" unless body.nil? || body.is_a?(Hash)

        options = request_options || {}
        bad = options.keys - REQUEST_OPTIONS
        raise ArgumentError, "#{op}: unknown request_options #{bad.inspect} (known: #{REQUEST_OPTIONS.join(", ")})" if bad.any?

        input = (body || {}).transform_keys(&:to_s).merge(fields.compact.transform_keys(&:to_s))
        @client.call(op, input, **options)
      end

      def unknown_fields_message(spec, unknown)
        listed = unknown.map do |name|
          near = DidYouMean::SpellChecker.new(dictionary: spec.fields).correct(name).first if defined?(DidYouMean)
          near ? "#{name} (did you mean #{near}?)" : name
        end
        "#{spec.name} has no field #{listed.join(", ")}; it takes #{spec.fields.join(", ")}"
      end
    end
  end
end
