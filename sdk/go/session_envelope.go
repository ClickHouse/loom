package loom

import (
	"encoding/json"
	"io"
	"strings"
	"unicode/utf8"
)

// A shared budget also bounds JSON documents embedded in MCP text blocks.
type sessionJSONBudget struct { nodes, text int }

func protectedEnvelope(raw []byte) (bool, error) {
	budget := &sessionJSONBudget{nodes: 10000, text: maxProtectedBody}
	value, err := budget.decode(string(raw))
	if err != nil {
		return false, err
	}
	root, ok := value.(map[string]any)
	if !ok {
		return false, ErrSessionProtectionUnavailable
	}
	pending := []map[string]any{root}
	protected := false
	for len(pending) != 0 {
		if len(pending) > 10000 {
			return false, ErrSessionProtectionUnavailable
		}
		item := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		if mirrored, exists := item["mirrored"]; exists {
			flag, ok := mirrored.(bool)
			if !ok {
				return false, ErrSessionProtectionUnavailable
			}
			protected = protected || flag
		}
		for _, key := range []string{"result", "structuredContent"} {
			if child, ok := item[key].(map[string]any); ok {
				pending = append(pending, child)
			}
		}
		if fault, ok := item["error"].(map[string]any); ok {
			if data, ok := fault["data"].(map[string]any); ok {
				pending = append(pending, data)
			}
		}
		if items, ok := item["items"].([]any); ok {
			for _, child := range items {
				if child, ok := child.(map[string]any); ok {
					pending = append(pending, child)
				}
			}
		}
		if content, ok := item["content"].([]any); ok {
			for _, block := range content {
				block, ok := block.(map[string]any)
				if !ok || block["type"] != "text" {
					continue
				}
				text, ok := block["text"].(string)
				if !ok {
					return false, ErrSessionProtectionUnavailable
				}
				budget.text -= len(text)
				if budget.text < 0 {
					return false, ErrSessionProtectionUnavailable
				}
				if strings.HasPrefix(strings.TrimSpace(text), "{") {
					decoded, err := budget.decode(text)
					if err != nil {
						return false, err
					}
					child, ok := decoded.(map[string]any)
					if !ok {
						return false, ErrSessionProtectionUnavailable
					}
					pending = append(pending, child)
				}
			}
		}
	}
	return protected, nil
}

func (budget *sessionJSONBudget) decode(text string) (any, error) {
	if !utf8.ValidString(text) || len(text) > maxProtectedBody {
		return nil, ErrSessionProtectionUnavailable
	}
	decoder := json.NewDecoder(strings.NewReader(text))
	decoder.UseNumber()
	value, err := budget.value(decoder, 0)
	if err != nil {
		return nil, err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, ErrSessionProtectionUnavailable
	}
	return value, nil
}

func (budget *sessionJSONBudget) value(decoder *json.Decoder, depth int) (any, error) {
	budget.nodes--
	if budget.nodes < 0 || depth > 64 {
		return nil, ErrSessionProtectionUnavailable
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	delim, compound := token.(json.Delim)
	if !compound {
		return token, nil
	}
	switch delim {
	case '{':
		object := make(map[string]any)
		for decoder.More() {
			key, err := decoder.Token()
			if err != nil {
				return nil, ErrSessionProtectionUnavailable
			}
			name, ok := key.(string)
			if !ok {
				return nil, ErrSessionProtectionUnavailable
			}
			if _, duplicate := object[name]; duplicate {
				return nil, ErrSessionProtectionUnavailable
			}
			child, err := budget.value(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			object[name] = child
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return nil, ErrSessionProtectionUnavailable
		}
		return object, nil
	case '[':
		var array []any
		for decoder.More() {
			child, err := budget.value(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			array = append(array, child)
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return nil, ErrSessionProtectionUnavailable
		}
		return array, nil
	default:
		return nil, ErrSessionProtectionUnavailable
	}
}
