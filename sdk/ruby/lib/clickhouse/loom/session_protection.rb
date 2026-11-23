# frozen_string_literal: true

require "json"
require "securerandom"

module ClickHouse
  module Loom
    class SessionProtectionUnavailable < StandardError
      def initialize
        super("Session protection state could not be confirmed")
      end
    end

    class AutomaticCaptureSuppressed < StandardError
      def initialize
        super("Automatic capture is suppressed for this session")
      end
    end

    # The application retains the pointer in its trusted conversation record.
    # The directory contains no response bodies or credentials.
    class SessionProtection
      MAX_BODY = 4 * 1024 * 1024
      CONTEXT = :loom_session_capture

      def self.create(path)
        Dir.mkdir(path, 0o700)
        id = SecureRandom.uuid
        { "identity" => id, "state" => "0" }.each do |name, value|
          File.open(File.join(path, name), File::WRONLY | File::CREAT | File::EXCL, 0o600) do |file|
            file.write(value)
            file.fsync
          end
        end
        sync_directory(path)
        open({ "path" => File.expand_path(path), "id" => id })
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def self.open(pointer)
        raise SessionProtectionUnavailable unless pointer.is_a?(Hash)

        family = new(pointer.fetch("path"), pointer.fetch("id"))
        family.ready!
        family.mark! if family.pending?
        family
      rescue KeyError, TypeError, SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def self.sync_directory(path)
        File.open(path, File::RDONLY) { |directory| directory.fsync }
      end

      def initialize(path, id)
        raise SessionProtectionUnavailable unless path.is_a?(String) && File.absolute_path(path) == path &&
                                                 id.is_a?(String) && id.match?(/\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\z/)

        @path = File.expand_path(path).freeze
        @id = id.dup.freeze
        @latched = false
        @mutex = Mutex.new
      end
      private_class_method :new

      def pointer
        { "path" => @path, "id" => @id }
      end

      def ready!
        @mutex.synchronize do
          directory = File.lstat(@path)
          raise SessionProtectionUnavailable unless directory.directory? && directory.uid == Process.uid &&
                                                   (directory.mode & 0o077).zero?
          raise SessionProtectionUnavailable unless read_private("identity", 36) == @id

          state = read_private("state", 1)
          raise SessionProtectionUnavailable unless ["0", "1"].include?(state)
          raise SessionProtectionUnavailable if @latched && state != "1"

          @latched ||= state == "1"
        end
        self
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def protected?
        ready!
        @latched
      end

      def mark!
        ready!
        @mutex.synchronize do
          @latched = true
          File.open(File.join(@path, "state"), File::WRONLY | File::NOFOLLOW) do |file|
            file.write("1")
            file.fsync
          end
        end
        self
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def pending?
        pending_except?(nil)
      end

      def pending_except?(own)
        count = 0
        Dir.each_child(@path) do |name|
          count += 1
          raise SessionProtectionUnavailable if count > 10_000
          return true if name.start_with?("pending-") && File.join(@path, name) != own
        end
        false
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def begin_attempt
        ready!
        name = File.join(@path, "pending-#{SecureRandom.uuid}")
        File.open(name, File::WRONLY | File::CREAT | File::EXCL, 0o600) { |file| file.fsync }
        self.class.sync_directory(@path)
        name
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def attempt_ready!(name)
        ready!
        raise SessionProtectionUnavailable unless File.dirname(name) == @path &&
                                                 File.basename(name).start_with?("pending-")
        read_private(File.basename(name), 0)
        self
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def finish_attempt(name)
        attempt_ready!(name)
        File.unlink(name)
        self.class.sync_directory(@path)
      rescue SystemCallError, IOError
        raise SessionProtectionUnavailable
      end

      def capture
        SessionCapture.new([self, *SessionCapture.current_families])
      end

      def self.marked?(value)
        pending = [value]
        remaining = 10_000
        text_bytes = MAX_BODY
        until pending.empty?
          remaining -= 1
          raise SessionProtectionUnavailable if remaining.negative?

          item = pending.pop
          next unless item.is_a?(Hash)
          if item.key?("mirrored") && item["mirrored"] != true && item["mirrored"] != false
            raise SessionProtectionUnavailable
          end
          return true if item["mirrored"] == true

          %w[result structuredContent].each { |key| pending << item[key] if item[key].is_a?(Hash) }
          data = item["error"].is_a?(Hash) ? item["error"]["data"] : nil
          pending << data if data.is_a?(Hash)
          items = item["items"]
          pending.concat(items) if items.is_a?(Array)
          content = item["content"]
          if content.is_a?(Array)
            remaining -= content.length
            content.each do |block|
              next unless block.is_a?(Hash) && block["type"] == "text" && block["text"].is_a?(String)

              text_bytes -= block["text"].bytesize
              raise SessionProtectionUnavailable if text_bytes.negative?
              pending << JSON.parse(block["text"]) if block["text"].lstrip.start_with?("{")
            end
          end
          raise SessionProtectionUnavailable if pending.length > remaining
        end
        false
      rescue JSON::ParserError, JSON::NestingError
        raise SessionProtectionUnavailable
      end

      private

      def read_private(name, size)
        File.open(File.join(@path, name), File::RDWR | File::NOFOLLOW) do |file|
          stat = file.stat
          raise SessionProtectionUnavailable unless stat.file? && stat.uid == Process.uid &&
                                                   (stat.mode & 0o077).zero? && stat.size == size
          file.read(size + 1)
        end
      end
    end

    # Retain this object when scheduling work; call it again in the later callback.
    class SessionCapture
      def self.current
        Thread.current[SessionProtection::CONTEXT]
      end

      def self.current_families
        current ? current.families : []
      end

      def initialize(families)
        unless families.is_a?(Array) && !families.empty? && families.all? { |family| family.is_a?(SessionProtection) }
          raise ArgumentError, "a capture requires contributing SessionProtection handles"
        end
        @families = families.uniq { |family| family.pointer }.freeze
      end

      attr_reader :families

      def contribute(family)
        self.class.new([*@families, family, *self.class.current_families])
      end

      def guard!
        @families.each do |family|
          raise AutomaticCaptureSuppressed if family.protected? || family.pending?
        end
      end

      def call
        previous = self.class.current
        retained = self.class.new([*@families, *self.class.current_families])
        retained.guard!
        Thread.current[SessionProtection::CONTEXT] = retained
        yield
      ensure
        Thread.current[SessionProtection::CONTEXT] = previous
      end
    end
  end
end
