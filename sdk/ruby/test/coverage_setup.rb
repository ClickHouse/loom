# frozen_string_literal: true

require "simplecov"
require "simplecov-lcov"

SimpleCov::Formatter::LcovFormatter.config do |config|
  config.report_with_single_file = true
  config.single_report_path = "coverage/lcov.info"
end

SimpleCov.start do
  track_files "lib/**/*.rb"
  add_filter "/test/"
  formatter SimpleCov::Formatter::LcovFormatter
end
