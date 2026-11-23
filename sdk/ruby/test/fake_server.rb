# frozen_string_literal: true

require "socket"

# A local HTTP server that answers each request with the next queued response and
# records what it was sent, so the tests run without a Loom.
class FakeServer
  Request = Struct.new(:method, :path, :headers, :body)

  attr_reader :requests

  def initialize
    @server = TCPServer.new("127.0.0.1", 0)
    @requests = []
    @responses = []
    @thread = Thread.new { serve }
  end

  def base_url
    "http://127.0.0.1:#{@server.addr[1]}"
  end

  # Queues the answer to the next request, written after +delay+ seconds.
  def respond(status, body, headers = {}, delay: 0)
    @responses << [status, body, headers, delay]
  end

  def close
    @server.close
    @thread.join(1)
  end

  private

  def serve
    loop do
      client = @server.accept
      # One thread per connection, so a delayed answer does not hold up the next request.
      Thread.new(client) { |c| handle(c) }
    end
  rescue IOError, Errno::EBADF
    nil
  end

  def handle(client)
    request_line = client.gets
    return if request_line.nil? # a client that connected and gave up

    method, path, = request_line.split
    headers = {}
    while (line = client.gets) && line != "\r\n"
      name, value = line.split(":", 2)
      headers[name.downcase] = value.strip
    end
    body = client.read(headers.fetch("content-length", "0").to_i).to_s
    @requests << Request.new(method, path, headers, body)
    status, payload, extra, delay = @responses.shift || [500, "no response queued", {}, 0]
    sleep(delay) if delay.positive?
    head = ["HTTP/1.1 #{status} Status", "Content-Length: #{payload.bytesize}", "Connection: close"]
    extra.each { |name, value| head << "#{name}: #{value}" }
    client.write("#{head.join("\r\n")}\r\n\r\n#{payload}")
  ensure
    client.close
  end
end
