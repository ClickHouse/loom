package loom

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Category says whose move an error is.
type Category string

// The categories the server names in detail.category.
const (
	CategoryInput         Category = "input"
	CategoryAuth          Category = "auth"
	CategoryCapacity      Category = "capacity"
	CategoryDependency    Category = "dependency"
	CategoryConfiguration Category = "configuration"
	CategoryInternal      Category = "internal"
)

// CodeInvalidResponse is the Code of an Error for a 2xx answer whose body is not JSON.
const CodeInvalidResponse = "INVALID_RESPONSE"

// Error is a non-2xx answer from the server, or a 2xx answer that is not JSON.
// Match on Code, never on Message: the code is the contract.
type Error struct {
	// Op is the operation called, like "memory.get".
	Op     string
	Status int
	// Code is the server's detail.code, like "RESOURCE_NOT_FOUND" or "RATE_LIMITED".
	Code    string
	Message string
	// Category and Retryable come from the server's detail, else from the HTTP status.
	Category  Category
	Retryable bool
	// RetryAfter is how long to wait before resending; zero when the server named no wait.
	RetryAfter time.Duration
	// RequestID is the server's X-Request-Id; quote it in a bug report.
	RequestID string
}

func (e *Error) Error() string {
	if e.Code == "" {
		return fmt.Sprintf("%s: HTTP %d: %s", e.Op, e.Status, e.Message)
	}
	return fmt.Sprintf("%s: HTTP %d %s: %s", e.Op, e.Status, e.Code, e.Message)
}

type errorDetail struct {
	Code        string   `json:"code"`
	Message     string   `json:"message"`
	Category    string   `json:"category"`
	Retryable   *bool    `json:"retryable"`
	RetryAfterS *float64 `json:"retry_after_s"`
}

func errorFromResponse(op string, status int, body []byte, requestID, retryAfter string) *Error {
	var detail errorDetail
	var envelope struct {
		Detail json.RawMessage `json:"detail"`
	}
	switch {
	case json.Unmarshal(body, &envelope) == nil && len(envelope.Detail) > 0:
		if err := json.Unmarshal(envelope.Detail, &detail); err != nil {
			// A handler that refused with a plain string detail.
			var text string
			if json.Unmarshal(envelope.Detail, &text) == nil {
				detail.Message = text
			}
		}
	case json.Unmarshal(body, &detail) == nil:
		// The fields at the top level, with no detail wrapper.
	default:
		// Not JSON at all: a proxy or load balancer answered.
		detail.Message = snippet(body, 500)
	}

	e := &Error{Op: op, Status: status, Code: detail.Code, Message: detail.Message, RequestID: requestID}
	if e.Message == "" {
		e.Message = fmt.Sprintf("HTTP %d", status)
	}
	e.Category, e.Retryable = classByStatus(status)
	if c := Category(detail.Category); isCategory(c) {
		e.Category = c
	}
	if detail.Retryable != nil {
		e.Retryable = *detail.Retryable
	}
	switch {
	case detail.RetryAfterS != nil:
		e.RetryAfter = seconds(*detail.RetryAfterS)
	case retryAfter != "":
		if s, err := strconv.ParseFloat(strings.TrimSpace(retryAfter), 64); err == nil {
			e.RetryAfter = seconds(s)
		}
	}
	return e
}

// classByStatus is the server's own rule for an error whose body names no category,
// such as a proxy's page or an older server.
func classByStatus(status int) (Category, bool) {
	switch {
	case status == 401 || status == 403:
		return CategoryAuth, false
	case status == 408 || status == 429:
		return CategoryCapacity, true
	case status == 502 || status == 503 || status == 504:
		return CategoryDependency, true
	case status >= 500:
		return CategoryInternal, false
	default:
		return CategoryInput, false
	}
}

func isCategory(c Category) bool {
	switch c {
	case CategoryInput, CategoryAuth, CategoryCapacity, CategoryDependency, CategoryConfiguration, CategoryInternal:
		return true
	}
	return false
}

func seconds(s float64) time.Duration {
	if s <= 0 {
		return 0
	}
	return time.Duration(s * float64(time.Second))
}

// snippet returns at most n bytes of b as text, cut on a rune boundary.
func snippet(b []byte, n int) string {
	if len(b) > n {
		b = b[:n]
		for len(b) > 0 && !utf8.Valid(b) {
			b = b[:len(b)-1]
		}
	}
	return strings.TrimSpace(string(b))
}
