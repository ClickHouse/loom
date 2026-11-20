package loom

import (
	"context"
	"encoding/json"
)

// SetOptions are optional fields of memory.set.
type SetOptions struct {
	// MemoryType classifies the fact; blank lets the server use "semantic".
	MemoryType string
	Tags       []string
}

// SetResult is what memory.set answers.
type SetResult struct {
	MemoryID string `json:"memory_id"`
	// Version is the nanosecond version the server assigned to the row.
	Version int64 `json:"version"`
	// ContentSHA256 is the SHA-256 of the value just written.
	ContentSHA256 string `json:"content_sha256"`
	// Status is the row's lifecycle status (active, duplicate, proposed);
	// blank on a write that stored no row.
	Status string `json:"status"`
	// Noop is true when the call wrote nothing because the store already held the fact.
	Noop bool `json:"noop"`
}

// Set saves value under key: plain text, or anything that encodes to a JSON object,
// list or scalar. Writing a key that already holds a row adds a new version.
func (c *Client) Set(ctx context.Context, key string, value any, opts *SetOptions) (*SetResult, error) {
	in := struct {
		identity
		MemoryKey  string   `json:"memory_key"`
		Value      any      `json:"value"`
		MemoryType string   `json:"memory_type,omitempty"`
		Tags       []string `json:"tags,omitempty"`
	}{identity: c.identity, MemoryKey: key, Value: value}
	if opts != nil {
		in.MemoryType, in.Tags = opts.MemoryType, opts.Tags
	}
	var out SetResult
	if err := c.call(ctx, "memory.set", in, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// Memory is what memory.get answers.
type Memory struct {
	// Found is false when no live row holds the key; the other fields are then empty.
	Found    bool   `json:"found"`
	MemoryID string `json:"memory_id"`
	// Value is the stored value as JSON; decode it with json.Unmarshal.
	Value      json.RawMessage `json:"value"`
	MemoryType string          `json:"memory_type"`
	Version    int64           `json:"version"`
	Tags       []string        `json:"tags"`
}

// Get reads the live row of key: under the client's agent first, then the
// same user's row under any agent. A missing key is not an error: Found is false.
func (c *Client) Get(ctx context.Context, key string) (*Memory, error) {
	in := struct {
		identity
		MemoryKey string `json:"memory_key"`
	}{identity: c.identity, MemoryKey: key}
	var out Memory
	if err := c.call(ctx, "memory.get", in, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SearchOptions are optional fields of memory.search.
type SearchOptions struct {
	// TopK caps the hits returned; zero lets the server use 5.
	TopK int
	// MemoryType restricts the search to one type; blank searches every searchable type.
	MemoryType string
	// Tags keeps only rows carrying any of these tags.
	Tags []string
}

// SearchHit is one ranked memory.
type SearchHit struct {
	MemoryKey      string  `json:"memory_key"`
	MemoryType     string  `json:"memory_type"`
	ContentExcerpt string  `json:"content_excerpt"`
	Similarity     float64 `json:"similarity"`
	Version        int64   `json:"version"`
}

// SearchResult is what memory.search answers.
type SearchResult struct {
	// Results are the ranked hits, best first; empty when nothing passed the noise floor.
	Results []SearchHit `json:"results"`
}

// Search ranks the memories that answer query. With no agent bound, it reads every
// agent's rows in the namespace; with one, only that agent's.
func (c *Client) Search(ctx context.Context, query string, opts *SearchOptions) (*SearchResult, error) {
	in := struct {
		identity
		Query      string   `json:"query"`
		TopK       int      `json:"top_k,omitempty"`
		MemoryType string   `json:"memory_type,omitempty"`
		Tags       []string `json:"tags,omitempty"`
	}{identity: c.identity, Query: query}
	if opts != nil {
		in.TopK, in.MemoryType, in.Tags = opts.TopK, opts.MemoryType, opts.Tags
	}
	var out SearchResult
	if err := c.call(ctx, "memory.search", in, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// DeleteResult is what memory.delete answers.
type DeleteResult struct {
	// Found is false when no live row of the key exists in the namespace.
	Found bool `json:"found"`
	// Tombstoned is true when a live row was found and a tombstone written over it.
	Tombstoned bool  `json:"tombstoned"`
	Version    int64 `json:"version"`
}

// Delete tombstones the live row of key, tried under the client's agent first,
// then the same user's row under any agent.
func (c *Client) Delete(ctx context.Context, key string) (*DeleteResult, error) {
	in := struct {
		identity
		MemoryKey string `json:"memory_key"`
	}{identity: c.identity, MemoryKey: key}
	var out DeleteResult
	if err := c.call(ctx, "memory.delete", in, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
