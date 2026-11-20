using System.Text.Json;

namespace ClickHouse.Loom;

/// <summary>A non-2xx answer from Loom, or a call that got no answer.</summary>
/// <remarks>
/// Match on <see cref="Code"/> (for example <c>NOT_FOUND</c> or <c>RATE_LIMITED</c>), never on the
/// message. <see cref="Category"/> says whose move it is (<c>input</c>, <c>auth</c>, <c>capacity</c>,
/// <c>dependency</c>, <c>configuration</c> or <c>internal</c>) and <see cref="Retryable"/> whether
/// resending the same request can succeed later. When the body names neither, both come from the
/// HTTP status, as docs/reference/errors.md describes.
/// </remarks>
public sealed class LoomException : Exception
{
    internal LoomException(
        string op,
        int status,
        string code,
        string message,
        string category,
        bool retryable,
        double? retryAfterSeconds,
        string requestId,
        JsonElement? detail,
        Exception? inner)
        : base(message, inner)
    {
        Op = op;
        Status = status;
        Code = code;
        Category = category;
        Retryable = retryable;
        RetryAfterSeconds = retryAfterSeconds;
        RequestId = requestId;
        Detail = detail ?? Json.EmptyObject;
    }

    /// <summary>The op that failed, for example <c>memory.get</c>.</summary>
    public string Op { get; }

    /// <summary>The HTTP status; 0 when no answer came (a connection failure or a timeout).</summary>
    public int Status { get; }

    /// <summary>The server's <c>detail.code</c>; empty when the answer named none.</summary>
    public string Code { get; }

    public string Category { get; }

    public bool Retryable { get; }

    /// <summary>Seconds to wait before resending (<c>detail.retry_after_s</c>, else <c>Retry-After</c>).</summary>
    public double? RetryAfterSeconds { get; }

    /// <summary>The <c>X-Request-Id</c> of the call, which finds it in the server's log.</summary>
    public string RequestId { get; }

    /// <summary>The whole <c>detail</c> object, for the fields a code adds (such as <c>current_sha256</c>).</summary>
    public JsonElement Detail { get; }

    /// <summary>The error for a non-2xx answer: the envelope's fields, else the status's class.</summary>
    internal static LoomException FromResponse(
        string op, int status, JsonElement? body, string requestId, double? retryAfterHeader)
    {
        JsonElement detail = Json.EmptyObject;
        if (body is { ValueKind: JsonValueKind.Object } b && b.TryGetProperty("detail", out var d))
        {
            if (d.ValueKind == JsonValueKind.String)
            {
                detail = JsonSerializer.SerializeToElement(new Dictionary<string, string?> { ["message"] = d.GetString() });
            }
            else if (d.ValueKind == JsonValueKind.Object)
            {
                detail = d;
            }
        }
        var message = Json.String(detail, "message");
        var retryable = detail.TryGetProperty("retryable", out var r) && r.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? r.GetBoolean()
            : RetryableOf(status);
        return new LoomException(
            op,
            status,
            Json.String(detail, "code") ?? "",
            string.IsNullOrEmpty(message) ? $"{op}: HTTP {status}" : message,
            Json.String(detail, "category") ?? CategoryOf(status),
            retryable,
            Json.Double(detail, "retry_after_s") ?? retryAfterHeader,
            requestId,
            detail,
            null);
    }

    /// <summary>The class docs/reference/errors.md gives a status whose body names none.</summary>
    internal static string CategoryOf(int status) => status switch
    {
        401 or 403 => "auth",
        408 or 429 => "capacity",
        502 or 503 or 504 => "dependency",
        >= 500 => "internal",
        _ => "input",
    };

    internal static bool RetryableOf(int status) => status is 408 or 429 or 502 or 503 or 504;
}
