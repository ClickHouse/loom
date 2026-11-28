# frozen_string_literal: true

require "uri"

module ClickHouse
  module Loom
    # Where a Client's bearer token comes from, beyond a static API key.
    #
    # A +token_provider+ is any object with +token(refused)+ (or a Proc taking +refused+)
    # that returns the token to send, as a String. The client asks it before every
    # request. After a 401 it asks once more, passing the token the server refused in
    # +refused+, and sends the call again with the answer; a provider that has no fresh
    # token returns the refused one, and the 401 is raised. An empty token sends no
    # Authorization header. A provider that responds to +origin+ ("https://loom.example")
    # is only ever used with a client for that origin. This is the TypeScript SDK's
    # TokenProvider: a bot's machine login or a stored `loom login` session plugs in here.
    #
    # The last credential a client looks for is the first line of ~/.loom/token, which
    # `loom mint-token --save` writes, as the TypeScript and Python SDKs read it.
    module Credentials
      # The file `loom mint-token --save` writes, under the user's home directory.
      TOKEN_FILE = File.join("~", ".loom", "token")

      module_function

      # The first line of ~/.loom/token (or +path+), stripped; nil when the file is
      # missing, unreadable or blank, or no home directory is known.
      def token_from_file(path = nil)
        text = File.read(File.expand_path(path || TOKEN_FILE), encoding: "UTF-8")
        line = text.strip.lines.first.to_s.strip
        line.empty? ? nil : line
      rescue SystemCallError, IOError, ArgumentError, EncodingError
        nil
      end

      # The provider's token, with +refused+ the token the server just answered 401 to
      # (nil on a first try).
      def provider_token(provider, refused)
        token =
          if provider.respond_to?(:token)
            provider.token(refused)
          elsif provider.respond_to?(:call)
            provider.arity.zero? ? provider.call : provider.call(refused)
          else
            raise ArgumentError, "token_provider must respond to token(refused) or call(refused)"
          end
        token.to_s.strip
      end

      # (scheme, host, port) of +url+, as a browser compares origins.
      def origin_of(url)
        uri = URI(url.to_s)
        [uri.scheme.to_s.downcase, uri.host.to_s.downcase, uri.port]
      rescue URI::InvalidURIError
        nil
      end

      # Raises ArgumentError when +provider+ is bound to another origin than +base_url+:
      # its token is never sent to a server it was not made for.
      def check_origin!(provider, base_url)
        return unless provider.respond_to?(:origin) && provider.origin

        bound = origin_of(provider.origin)
        return if bound && bound == origin_of(base_url)

        label = provider.respond_to?(:label) && provider.label ? provider.label : "the token provider"
        raise ArgumentError, "refusing to send #{label}'s token to #{URI(base_url).host}: it is bound to " \
                             "#{provider.origin}"
      end
    end
  end
end
