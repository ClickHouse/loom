using System.Text.Json;

namespace ClickHouse.Loom;

/// <summary><c>memory.set</c>: the version and id the server assigned, and the whole answer as <c>Raw</c>.</summary>
public sealed record SetResult(string MemoryId, long Version, string ContentSha256, string Status, bool Pinned, JsonElement Raw)
{
    internal static SetResult From(JsonElement r) => new(
        Json.String(r, "memory_id") ?? "",
        Json.Long(r, "version") ?? 0,
        Json.String(r, "content_sha256") ?? "",
        Json.String(r, "status") ?? "",
        Json.Bool(r, "pinned"),
        r);
}

/// <summary>
/// <c>memory.get</c>: <c>Found</c> is false when no live row holds the key. <c>Value</c> is the stored
/// value as written: text, an object, a list or a scalar.
/// </summary>
public sealed record GetResult(
    bool Found,
    string MemoryId,
    JsonElement? Value,
    string? MemoryType,
    long? Version,
    string? VersionText,
    JsonElement Raw)
{
    internal static GetResult From(JsonElement r) => new(
        Json.Bool(r, "found"),
        Json.String(r, "memory_id") ?? "",
        r.TryGetProperty("value", out var v) ? v : null,
        Json.String(r, "memory_type"),
        Json.Long(r, "version"),
        Json.String(r, "version_text"),
        r);
}

/// <summary>One <c>memory.search</c> hit, best first.</summary>
public sealed record SearchHit(
    string MemoryKey,
    string MemoryType,
    string ContentExcerpt,
    double Similarity,
    double RankScore,
    JsonElement Raw)
{
    internal static SearchHit From(JsonElement h) => new(
        Json.String(h, "memory_key") ?? "",
        Json.String(h, "memory_type") ?? "",
        Json.String(h, "content_excerpt") ?? "",
        Json.Double(h, "similarity") ?? 0,
        Json.Double(h, "rank_score") ?? 0,
        h);
}

/// <summary><c>memory.search</c>: the ranked hits; empty when nothing passed the noise floor.</summary>
public sealed record SearchResult(IReadOnlyList<SearchHit> Hits, JsonElement Raw)
{
    internal static SearchResult From(JsonElement r) => new(
        r.TryGetProperty("results", out var hits) && hits.ValueKind == JsonValueKind.Array
            ? hits.EnumerateArray().Select(SearchHit.From).ToList()
            : [],
        r);
}

/// <summary><c>memory.delete</c>: <c>Found</c> is false when no live row of the key exists.</summary>
public sealed record DeleteResult(bool Tombstoned, bool Found, long Version, JsonElement Raw)
{
    internal static DeleteResult From(JsonElement r) => new(
        Json.Bool(r, "tombstoned"),
        Json.Bool(r, "found"),
        Json.Long(r, "version") ?? 0,
        r);
}

/// <summary>Typed reads of an answer's fields; a missing or differently typed field reads as absent.</summary>
internal static class Json
{
    internal static readonly JsonElement EmptyObject = JsonSerializer.SerializeToElement(new Dictionary<string, object>());

    internal static string? String(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    internal static long? Long(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt64(out var n) ? n : null;

    internal static double? Double(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;

    internal static bool Bool(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.True;
}
