# frozen_string_literal: true

module ClickHouse
  module Loom
    # Cursor pagination over the wire's two page shapes (+rows+ or +results+, with
    # +next_cursor+), as paginate in the TypeScript SDK.
    module Pagination
      module_function

      # Walks a cursor-paginated op (knowledge.list, conversation.list, audit.list,
      # code.search) until the server stops returning +next_cursor+. The block receives the
      # cursor to send (nil for the first page) and returns the decoded page.
      #
      # Returns a lazy Enumerator of the rows: a page is fetched only when its rows are
      # needed, so +first(n)+ stops fetching once it has n rows. Reading stops after
      # +max_pages+ pages (no limit when nil), and when a page hands back the cursor it was
      # fetched with, so a server that repeats a cursor cannot loop forever.
      def paginate(max_pages: nil, &fetch_page)
        raise ArgumentError, "paginate needs a block that fetches one page" unless fetch_page
        unless max_pages.nil? || max_pages.is_a?(Integer)
          raise ArgumentError, "max_pages must be an Integer or nil, got #{max_pages.inspect}"
        end

        Enumerator.new do |rows|
          cursor = nil
          pages = 0
          while max_pages.nil? || pages < max_pages
            page = fetch_page.call(cursor)
            pages += 1
            page_rows(page).each { |row| rows << row }
            following = page_value(page, "next_cursor")
            break if following.nil? || following == "" || following == cursor

            cursor = following
          end
        end
      end

      # The rows of a page: +rows+, else +results+, else none.
      def page_rows(page)
        page_value(page, "rows") || page_value(page, "results") || []
      end

      # A page decoded from JSON has String keys; one built by hand may use Symbols.
      def page_value(page, key)
        page.key?(key) ? page[key] : page[key.to_sym]
      end
      private_class_method :page_rows, :page_value
    end
  end
end
