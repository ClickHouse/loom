# frozen_string_literal: true

require_relative "loom/version"
require_relative "loom/errors"
require_relative "loom/client"

# The short name the issue and the docs use (Loom.configure, Loom::RateLimited), unless
# the application already has a Loom of its own.
Loom = ClickHouse::Loom unless Object.const_defined?(:Loom)
