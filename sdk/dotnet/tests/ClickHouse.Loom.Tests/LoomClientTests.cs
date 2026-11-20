using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Xunit;

namespace ClickHouse.Loom.Tests;

/// <summary>Against a stubbed transport: what each call sends, what it reads back, and the errors.</summary>
public class LoomClientTests
{
    private readonly Stub _stub = new();

    private LoomClient Client(string token = "") => new(
        new LoomClientOptions { BaseUrl = "http://loom.test/", Token = token, Org = "dev", Namespace = "local", Agent = "dotnet-test" },
        new HttpClient(_stub));

    [Fact]
    public async Task SetSendsTheIdentityKeyAndValueAndReadsTheVersion()
    {
        _stub.Answer(200, """{"version":1731888000000000001,"memory_id":"m-1","content_sha256":"ab","status":"active","pinned":false}""");
        var r = await Client("tok").SetAsync(
            "seat_pref",
            new Dictionary<string, string> { ["seat"] = "aisle" },
            new Dictionary<string, object?> { ["tags"] = new[] { "travel" } });

        var s = _stub.Seen.Single();
        Assert.Equal(HttpMethod.Post, s.Method);
        Assert.Equal("http://loom.test/v1/memory.set", s.Url);
        Assert.Equal("Bearer tok", s.Header("Authorization"));
        Assert.Equal("application/json", s.ContentType);
        Assert.Equal("application/json", s.Header("Accept"));
        Assert.Equal("loom-dotnet/0.1.0", s.Header("User-Agent"));
        Assert.Matches("^[0-9a-f-]{36}$", s.Header("X-Request-Id"));
        Assert.Equal("dev", s.Body.GetProperty("org").GetString());
        Assert.Equal("local", s.Body.GetProperty("namespace").GetString());
        Assert.Equal("dotnet-test", s.Body.GetProperty("agent").GetString());
        Assert.Equal("seat_pref", s.Body.GetProperty("memory_key").GetString());
        Assert.Equal("aisle", s.Body.GetProperty("value").GetProperty("seat").GetString());
        Assert.Equal("travel", s.Body.GetProperty("tags")[0].GetString());

        Assert.Equal(1731888000000000001L, r.Version); // above 2^53, exact as a long
        Assert.Equal("m-1", r.MemoryId);
        Assert.Equal("active", r.Status);
    }

    [Fact]
    public async Task NoTokenSendsNoAuthorizationHeader()
    {
        _stub.Answer(200, """{"found":false}""");
        await Client().GetAsync("k");
        Assert.Null(_stub.Seen.Single().Header("Authorization"));
    }

    [Fact]
    public async Task GetReadsTheValueAsWrittenOrNotFound()
    {
        _stub.Answer(200, """{"found":true,"memory_id":"m-2","value":{"seat":"aisle"},"memory_type":"fact","version":7}""");
        var r = await Client().GetAsync("seat_pref");
        Assert.Equal("/v1/memory.get", new Uri(_stub.Seen[0].Url).AbsolutePath);
        Assert.Equal("seat_pref", _stub.Seen[0].Body.GetProperty("memory_key").GetString());
        Assert.True(r.Found);
        Assert.Equal("aisle", r.Value!.Value.GetProperty("seat").GetString());
        Assert.Equal("fact", r.MemoryType);
        Assert.Equal(7L, r.Version);

        _stub.Answer(200, """{"found":false,"version":null}""");
        var missing = await Client().GetAsync("nothing_here");
        Assert.False(missing.Found);
        Assert.Null(missing.Value);
        Assert.Null(missing.Version);
    }

    [Fact]
    public async Task SearchSendsQueryAndTopKAndReadsHitsInOrder()
    {
        _stub.Answer(200, """
            {"results":[
              {"memory_key":"a","memory_type":"fact","content_excerpt":"x","similarity":0.9,"rank_score":0.8},
              {"memory_key":"b","memory_type":"note","content_excerpt":"y","similarity":0.5,"rank_score":0.4}]}
            """);
        var r = await Client().SearchAsync("which seat?", 5);
        var s = _stub.Seen.Single();
        Assert.Equal("/v1/memory.search", new Uri(s.Url).AbsolutePath);
        Assert.Equal("which seat?", s.Body.GetProperty("query").GetString());
        Assert.Equal(5, s.Body.GetProperty("top_k").GetInt32());
        Assert.Equal(new[] { "a", "b" }, r.Hits.Select(h => h.MemoryKey));
        Assert.Equal(0.9, r.Hits[0].Similarity);
        Assert.Equal(0.4, r.Hits[1].RankScore);
    }

    [Fact]
    public async Task DeleteSendsTheKeyAndReadsTheTombstone()
    {
        _stub.Answer(200, """{"tombstoned":true,"found":true,"version":9}""");
        var r = await Client().DeleteAsync("seat_pref");
        var s = _stub.Seen.Single();
        Assert.Equal("/v1/memory.delete", new Uri(s.Url).AbsolutePath);
        Assert.Equal("seat_pref", s.Body.GetProperty("memory_key").GetString());
        Assert.True(r.Tombstoned);
        Assert.True(r.Found);
        Assert.Equal(9L, r.Version);
    }

    [Theory]
    [InlineData(422, "VALIDATION_ERROR", "input", false)]
    [InlineData(403, "NAMESPACE_NOT_ACCESSIBLE", "auth", false)]
    [InlineData(429, "RATE_LIMITED", "capacity", true)]
    [InlineData(503, "BACKEND_UNAVAILABLE", "dependency", true)]
    [InlineData(503, "SCHEMA_PENDING", "configuration", false)]
    [InlineData(500, "INTERNAL_ERROR", "internal", false)]
    public async Task AnErrorCarriesTheEnvelopesCodeCategoryAndRetryable(int status, string code, string category, bool retryable)
    {
        _stub.Answer(
            status,
            JsonSerializer.Serialize(new { detail = new { code, message = "m", category, retryable, retry_after_s = 2 } }),
            ("X-Request-Id", "req-1"));
        var e = await Assert.ThrowsAsync<LoomException>(() => Client().GetAsync("k"));
        Assert.Equal(status, e.Status);
        Assert.Equal(code, e.Code);
        Assert.Equal(category, e.Category);
        Assert.Equal(retryable, e.Retryable);
        Assert.Equal(2.0, e.RetryAfterSeconds);
        Assert.Equal("req-1", e.RequestId);
        Assert.Equal("memory.get", e.Op);
        Assert.Equal("m", e.Message);
        Assert.Equal(code, e.Detail.GetProperty("code").GetString());
    }

    [Fact]
    public async Task NotFoundIsMatchedByCode()
    {
        _stub.Answer(404, """{"detail":{"code":"NOT_FOUND","message":"no such key","category":"input","retryable":false}}""");
        var e = await Assert.ThrowsAsync<LoomException>(() => Client().DeleteAsync("k"));
        Assert.Equal("NOT_FOUND", e.Code);
        Assert.False(e.Retryable);
    }

    [Theory]
    [InlineData(400, "input", false)]
    [InlineData(401, "auth", false)]
    [InlineData(403, "auth", false)]
    [InlineData(408, "capacity", true)]
    [InlineData(429, "capacity", true)]
    [InlineData(502, "dependency", true)]
    [InlineData(503, "dependency", true)]
    [InlineData(504, "dependency", true)]
    [InlineData(500, "internal", false)]
    public async Task ABodyWithoutAnEnvelopeIsClassedByStatus(int status, string category, bool retryable)
    {
        _stub.Answer(status, "<html>proxy error</html>", ("Retry-After", "30"));
        var e = await Assert.ThrowsAsync<LoomException>(() => Client().SetAsync("k", "v"));
        Assert.Equal("", e.Code);
        Assert.Equal(category, e.Category);
        Assert.Equal(retryable, e.Retryable);
        Assert.Equal(30.0, e.RetryAfterSeconds);
        Assert.Equal("<html>proxy error</html>", e.Message);
        Assert.Equal(_stub.Seen[0].Header("X-Request-Id"), e.RequestId);
    }

    [Fact]
    public async Task ATextDetailIsTheMessage()
    {
        _stub.Answer(401, """{"detail":"Not authenticated"}""");
        var e = await Assert.ThrowsAsync<LoomException>(() => Client().GetAsync("k"));
        Assert.Equal("Not authenticated", e.Message);
        Assert.Equal("auth", e.Category);
    }

    [Fact]
    public async Task ASuccessThatIsNotAJsonObjectIsABadResponse()
    {
        _stub.Answer(200, "[]");
        var e = await Assert.ThrowsAsync<LoomException>(() => Client().GetAsync("k"));
        Assert.Equal("BAD_RESPONSE", e.Code);
        Assert.Equal("internal", e.Category);
    }

    [Fact(Timeout = 10_000)]
    public async Task ATimeoutIsARetryableDependencyError()
    {
        _stub.Hang = true;
        // HttpClient's own 100 s timeout off, so only the client's Timeout can end the call.
        using var client = new LoomClient(
            new LoomClientOptions { BaseUrl = "http://loom.test", Token = "", Org = "dev", Namespace = "local", Agent = "a", Timeout = TimeSpan.FromMilliseconds(200) },
            new HttpClient(_stub) { Timeout = Timeout.InfiniteTimeSpan });
        var e = await Assert.ThrowsAsync<LoomException>(() => client.GetAsync("k"));
        Assert.Equal("TIMEOUT", e.Code);
        Assert.Equal(0, e.Status);
        Assert.Equal("dependency", e.Category);
        Assert.True(e.Retryable);
    }

    [Fact]
    public async Task CancellingTheCallIsNotALoomError()
    {
        _stub.Hang = true;
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => Client().GetAsync("k", cancellationToken: cancel.Token));
    }

    [Fact]
    public async Task AnUnreachableServerIsARetryableDependencyError()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        using var client = new LoomClient(
            new LoomClientOptions { BaseUrl = $"http://127.0.0.1:{port}", Token = "", Org = "dev", Namespace = "local", Agent = "a" });
        var e = await Assert.ThrowsAsync<LoomException>(() => client.GetAsync("k"));
        Assert.Equal("UNREACHABLE", e.Code);
        Assert.Equal(0, e.Status);
        Assert.Equal("dependency", e.Category);
        Assert.True(e.Retryable);
    }

    [Fact]
    public async Task TheClientsOwnHttpClientDoesNotFollowARedirect()
    {
        using var elsewhere = new RawServer("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
        using var loom = new RawServer(
            $"HTTP/1.1 307 Temporary Redirect\r\nLocation: {elsewhere.Url}/v1/memory.set\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
        using var client = new LoomClient(
            new LoomClientOptions { BaseUrl = loom.Url, Token = "tok", Org = "dev", Namespace = "local", Agent = "a" });
        var e = await Assert.ThrowsAsync<LoomException>(() => client.SetAsync("k", "private value"));
        Assert.Equal(307, e.Status);
        Assert.Single(loom.Requests);
        Assert.Empty(elsewhere.Requests);
    }

    [Fact]
    public void TheIdentityAndBaseUrlAreRequired()
    {
        Assert.Throws<ArgumentException>(() => new LoomClient(
            new LoomClientOptions { BaseUrl = "", Org = "o", Namespace = "n", Agent = "a" }));
        Assert.Throws<ArgumentException>(() => new LoomClient(
            new LoomClientOptions { BaseUrl = "http://loom.test", Org = "o", Namespace = "n", Agent = " " }));
    }
}

/// <summary>Reads LOOM_BASE_URL and LOOM_API_KEY, so it runs alone: environment variables are process-wide.</summary>
[CollectionDefinition(nameof(Environment), DisableParallelization = true)]
public class EnvironmentCollection;

[Collection(nameof(Environment))]
public class FromEnvironmentTests
{
    [Fact]
    public async Task FromEnvironmentReadsTheServerAndTokenOverHttp11()
    {
        using var server = new RawServer("HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"found\":false}");
        Environment.SetEnvironmentVariable("LOOM_BASE_URL", server.Url);
        Environment.SetEnvironmentVariable("LOOM_API_KEY", "env-token");
        try
        {
            using var client = LoomClient.FromEnvironment("dev", "local", "a");
            Assert.False((await client.GetAsync("k")).Found);
        }
        finally
        {
            Environment.SetEnvironmentVariable("LOOM_BASE_URL", null);
            Environment.SetEnvironmentVariable("LOOM_API_KEY", null);
        }
        var request = server.Requests.Single();
        Assert.StartsWith("POST /v1/memory.get HTTP/1.1\r\n", request);
        Assert.Contains("\r\nAuthorization: Bearer env-token\r\n", request);
    }
}

/// <summary>A transport that records each request and answers with a canned response.</summary>
internal sealed class Stub : HttpMessageHandler
{
    private int _status = 200;
    private string _answer = "{}";
    private (string Name, string Value)[] _headers = [];

    public List<Seen> Seen { get; } = [];

    public bool Hang { get; set; }

    public void Answer(int status, string body, params (string Name, string Value)[] headers) =>
        (_status, _answer, _headers) = (status, body, headers);

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var body = await request.Content!.ReadAsStringAsync(cancellationToken);
        Seen.Add(new Seen(
            request.Method,
            request.RequestUri!.ToString(),
            request.Headers.ToDictionary(h => h.Key, h => string.Join(", ", h.Value), StringComparer.OrdinalIgnoreCase),
            request.Content.Headers.ContentType?.MediaType,
            JsonDocument.Parse(body).RootElement.Clone()));
        if (Hang)
        {
            await Task.Delay(Timeout.Infinite, cancellationToken);
        }
        var response = new HttpResponseMessage((HttpStatusCode)_status) { Content = new StringContent(_answer, Encoding.UTF8) };
        foreach (var (name, value) in _headers)
        {
            response.Headers.TryAddWithoutValidation(name, value);
        }
        return response;
    }
}

internal sealed record Seen(HttpMethod Method, string Url, Dictionary<string, string> Headers, string? ContentType, JsonElement Body)
{
    public string? Header(string name) => Headers.GetValueOrDefault(name);
}

/// <summary>A local socket that records each raw HTTP request and sends one canned answer.</summary>
internal sealed class RawServer : IDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly byte[] _answer;
    private readonly List<string> _requests = [];

    public RawServer(string answer)
    {
        _answer = Encoding.ASCII.GetBytes(answer);
        _listener.Start();
        _ = ServeAsync();
    }

    public string Url => $"http://127.0.0.1:{((IPEndPoint)_listener.LocalEndpoint).Port}";

    public IReadOnlyList<string> Requests
    {
        get
        {
            lock (_requests)
            {
                return [.. _requests];
            }
        }
    }

    public void Dispose() => _listener.Stop();

    private async Task ServeAsync()
    {
        while (true)
        {
            TcpClient connection;
            try
            {
                connection = await _listener.AcceptTcpClientAsync();
            }
            catch (Exception e) when (e is SocketException or ObjectDisposedException)
            {
                return;
            }
            using (connection)
            {
                var stream = connection.GetStream();
                var request = await ReadRequestAsync(stream);
                lock (_requests)
                {
                    _requests.Add(request);
                }
                await stream.WriteAsync(_answer);
            }
        }
    }

    private static async Task<string> ReadRequestAsync(NetworkStream stream)
    {
        var received = new List<byte>();
        var buffer = new byte[4096];
        int headEnd;
        while ((headEnd = IndexOf(received, "\r\n\r\n"u8)) < 0)
        {
            var n = await stream.ReadAsync(buffer);
            if (n == 0)
            {
                break;
            }
            received.AddRange(buffer.AsSpan(0, n).ToArray());
        }
        var head = Encoding.ASCII.GetString(received.ToArray(), 0, Math.Max(headEnd, 0));
        var lengthLine = head.Split("\r\n").FirstOrDefault(l => l.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase));
        var length = lengthLine is null ? 0 : int.Parse(lengthLine["Content-Length:".Length..].Trim());
        while (headEnd >= 0 && received.Count < headEnd + 4 + length)
        {
            var n = await stream.ReadAsync(buffer);
            if (n == 0)
            {
                break;
            }
            received.AddRange(buffer.AsSpan(0, n).ToArray());
        }
        return Encoding.UTF8.GetString(received.ToArray());
    }

    private static int IndexOf(List<byte> haystack, ReadOnlySpan<byte> needle) =>
        haystack.ToArray().AsSpan().IndexOf(needle);
}
