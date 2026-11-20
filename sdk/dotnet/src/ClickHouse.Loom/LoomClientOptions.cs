namespace ClickHouse.Loom;

/// <summary>The server a <see cref="LoomClient"/> calls and the identity it stamps on every call.</summary>
public sealed class LoomClientOptions
{
    /// <summary>The server, for example <c>http://localhost:7777</c>; else <c>LOOM_BASE_URL</c>.</summary>
    public string? BaseUrl { get; init; }

    /// <summary>The bearer token; else <c>LOOM_API_KEY</c>. Blank means none, as on a single-user local server.</summary>
    public string? Token { get; init; }

    public required string Org { get; init; }

    public required string Namespace { get; init; }

    public required string Agent { get; init; }

    /// <summary>How long one call may take, its answer read included (default 30 s).</summary>
    public TimeSpan Timeout { get; init; } = TimeSpan.FromSeconds(30);
}
