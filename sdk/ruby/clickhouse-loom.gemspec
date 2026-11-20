# frozen_string_literal: true

require_relative "lib/clickhouse/loom/version"

Gem::Specification.new do |spec|
  spec.name = "clickhouse-loom"
  spec.version = ClickHouse::Loom::VERSION
  spec.summary = "A minimal Ruby client for ClickHouse Loom's memory operations."
  spec.description = "Saves, reads, searches and deletes agent memories over Loom's HTTP API, " \
                     "with the standard library only."
  spec.authors = ["ClickHouse"]
  spec.homepage = "https://github.com/ClickHouse/loom/tree/main/sdk/ruby"
  spec.license = "Apache-2.0"
  spec.required_ruby_version = ">= 3.1"
  spec.metadata = {
    "source_code_uri" => "https://github.com/ClickHouse/loom/tree/main/sdk/ruby",
    "bug_tracker_uri" => "https://github.com/ClickHouse/loom/issues"
  }
  spec.files = Dir["lib/**/*.rb"] + ["README.md"]
  spec.require_paths = ["lib"]
end
