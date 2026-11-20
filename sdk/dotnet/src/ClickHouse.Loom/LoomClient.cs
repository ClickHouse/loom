using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace ClickHouse.Loom;

/// <summary>
/// A minimal Loom client for the memory ops: <c>memory.set</c>, <c>memory.get</c>,
/// <c>memory.search</c> and <c>memory.delete</c>.
/// </summary>
/// <remarks>
/// Every op is <c>POST {BaseUrl}/v1/{op}</c> with a JSON body and, when a token is set,
/// <c>Authorization: Bearer {token}</c>. The identity (org, namespace, agent) is bound once and
/// stamped on every call. A non-2xx answer throws <see cref="LoomException"/>.
/// </remarks>
public sealed class LoomClient : IDisposable
{
    internal const string UserAgent = "loom-dotnet/0.1.0";

    private readonly string _baseUrl;
    private readonly string? _token;
    private readonly string _org;
    private readonly string _namespace;
    private readonly string _agent;
    private readonly TimeSpan _timeout;
    private readonly HttpClient _http;
    private readonly bool _ownsHttp;

    /// <param name="options">The server, the token and the identity.</param>
    /// <param name="httpClient">
    /// A preconfigured client (proxy, TLS, handler); its own redirect setting applies. Without one,
    /// the client makes its own, which does not follow redirects.
    /// </param>
    public LoomClient(LoomClientOptions options, HttpClient? httpClient = null)
    {
        ArgumentNullException.ThrowIfNull(options);
        var baseUrl = options.BaseUrl ?? Environment.GetEnvironmentVariable("LOOM_BASE_URL");
        if (string.IsNullOrWhiteSpace(baseUrl))
        {
            throw new ArgumentException("BaseUrl is required (or set LOOM_BASE_URL)", nameof(options));
        }
        if (options.Timeout <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(options), "Timeout must be positive");
        }
        _baseUrl = baseUrl.TrimEnd('/');
        var token = options.Token ?? Environment.GetEnvironmentVariable("LOOM_API_KEY");
        _token = string.IsNullOrWhiteSpace(token) ? null : token;
        _org = Required(options.Org, nameof(options.Org));
        _namespace = Required(options.Namespace, nameof(options.Namespace));
        _agent = Required(options.Agent, nameof(options.Agent));
        _timeout = options.Timeout;
        _ownsHttp = httpClient is null;
        _http = httpClient ?? new HttpClient(
            // A followed 307 or 308 would resend a memory write, value and all, to wherever it points.
            new SocketsHttpHandler { AllowAutoRedirect = false, ConnectTimeout = TimeSpan.FromSeconds(10) })
        {
            // Each call's own deadline is the options' Timeout, which may exceed HttpClient's 100 s default.
            Timeout = System.Threading.Timeout.InfiniteTimeSpan,
        };
    }

    /// <summary>A client from <c>LOOM_BASE_URL</c> and <c>LOOM_API_KEY</c>, with the identity given.</summary>
    public static LoomClient FromEnvironment(string org, string @namespace, string agent) =>
        new(new LoomClientOptions { Org = org, Namespace = @namespace, Agent = agent });

    /// <summary>Save a fact under <paramref name="memoryKey"/>; a key that holds a row gains a new version.</summary>
    /// <param name="memoryKey">The key.</param>
    /// <param name="value">Text, or anything System.Text.Json serializes.</param>
    /// <param name="extra">The op's other input fields (<c>tags</c>, <c>memory_type</c>, <c>user_id</c>, ...), named as docs/reference/operations/memory.md names them.</param>
    /// <param name="cancellationToken">Cancels the call.</param>
    public async Task<SetResult> SetAsync(
        string memoryKey,
        object value,
        IReadOnlyDictionary<string, object?>? extra = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(memoryKey);
        ArgumentNullException.ThrowIfNull(value);
        var body = Body(extra);
        body["memory_key"] = memoryKey;
        body["value"] = JsonSerializer.SerializeToNode(value);
        return SetResult.From(await CallAsync("memory.set", body, cancellationToken).ConfigureAwait(false));
    }

    /// <summary>Read one memory by its exact key; a key that holds nothing answers <c>Found</c> false.</summary>
    public async Task<GetResult> GetAsync(
        string memoryKey,
        IReadOnlyDictionary<string, object?>? extra = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(memoryKey);
        var body = Body(extra);
        body["memory_key"] = memoryKey;
        return GetResult.From(await CallAsync("memory.get", body, cancellationToken).ConfigureAwait(false));
    }

    /// <summary>Semantic search over the namespace's memories; at most <paramref name="topK"/> hits.</summary>
    public async Task<SearchResult> SearchAsync(
        string query,
        int topK,
        IReadOnlyDictionary<string, object?>? extra = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(query);
        var body = Body(extra);
        body["query"] = query;
        body["top_k"] = topK;
        return SearchResult.From(await CallAsync("memory.search", body, cancellationToken).ConfigureAwait(false));
    }

    /// <summary>Tombstone a memory by key (recoverable from the dashboard for 14 days).</summary>
    public async Task<DeleteResult> DeleteAsync(
        string memoryKey,
        IReadOnlyDictionary<string, object?>? extra = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(memoryKey);
        var body = Body(extra);
        body["memory_key"] = memoryKey;
        return DeleteResult.From(await CallAsync("memory.delete", body, cancellationToken).ConfigureAwait(false));
    }

    /// <summary>Disposes the <see cref="HttpClient"/> this client made; one passed in stays the caller's.</summary>
    public void Dispose()
    {
        if (_ownsHttp)
        {
            _http.Dispose();
        }
    }

    private JsonObject Body(IReadOnlyDictionary<string, object?>? extra)
    {
        var body = new JsonObject { ["org"] = _org, ["namespace"] = _namespace, ["agent"] = _agent };
        foreach (var (name, value) in extra ?? new Dictionary<string, object?>())
        {
            body[name] = JsonSerializer.SerializeToNode(value);
        }
        return body;
    }

    private async Task<JsonElement> CallAsync(string op, JsonObject body, CancellationToken cancellationToken)
    {
        var requestId = Guid.NewGuid().ToString();
        using var request = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl}/v1/{op}")
        {
            Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json"),
        };
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        request.Headers.UserAgent.ParseAdd(UserAgent);
        request.Headers.Add("X-Request-Id", requestId);
        if (_token is not null)
        {
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _token);
        }

        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(_timeout);
        int status;
        string text;
        string requestIdEchoed;
        double? retryAfter;
        try
        {
            using var response = await _http.SendAsync(request, deadline.Token).ConfigureAwait(false);
            status = (int)response.StatusCode;
            text = await response.Content.ReadAsStringAsync(deadline.Token).ConfigureAwait(false);
            requestIdEchoed = response.Headers.TryGetValues("X-Request-Id", out var ids) ? ids.First() : requestId;
            retryAfter = response.Headers.RetryAfter?.Delta?.TotalSeconds;
        }
        catch (OperationCanceledException e) when (!cancellationToken.IsCancellationRequested)
        {
            // The deadline, or a passed-in HttpClient's own Timeout; the caller's own cancellation propagates.
            throw NoAnswer(op, "TIMEOUT", $"timed out after {_timeout}", requestId, e);
        }
        catch (HttpRequestException e)
        {
            throw NoAnswer(op, "UNREACHABLE", "no answer: " + e.Message, requestId, e);
        }

        var parsed = Parse(text);
        if (status is < 200 or > 299)
        {
            if (parsed is null && text.Length > 0)
            {
                var excerpt = text.Length > 200 ? text[..200] : text;
                parsed = JsonSerializer.SerializeToElement(new Dictionary<string, string> { ["detail"] = excerpt });
            }
            throw LoomException.FromResponse(op, status, parsed, requestIdEchoed, retryAfter);
        }
        if (parsed is not { ValueKind: JsonValueKind.Object } answer)
        {
            throw new LoomException(
                op, status, "BAD_RESPONSE", $"{op}: the answer is not a JSON object", "internal", false, null,
                requestIdEchoed, null, null);
        }
        return answer;
    }

    private static LoomException NoAnswer(string op, string code, string message, string requestId, Exception inner) =>
        new(op, 0, code, $"{op}: {message}", "dependency", true, null, requestId, null, inner);

    private static JsonElement? Parse(string text)
    {
        if (text.Length == 0)
        {
            return null;
        }
        try
        {
            using var doc = JsonDocument.Parse(text);
            return doc.RootElement.Clone();
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string Required(string? value, string name) =>
        string.IsNullOrWhiteSpace(value) ? throw new ArgumentException($"{name} is required", name) : value;
}
