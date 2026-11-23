package loom

import (
	"context"
	"fmt"
)

// Call invokes an existing operation with its wire-schema arguments. Bound and
// retained families guard automatic calls, including calls through other clients.
// It does not retry. Unknown operations have no read-only exemption.
func (c *Client) Call(ctx context.Context, op string, arguments map[string]any, out any) error {
	in, err := c.operationArguments(arguments)
	if err != nil {
		return err
	}
	return c.call(ctx, op, in, out)
}

// CallHuman records explicit human intent for this invocation only. The caller
// must obtain that intent from the application user for each call. It still
// validates durable proof and observes responses. No authority enters ctx.
func (c *Client) CallHuman(ctx context.Context, op string, arguments map[string]any, out any) error {
	in, err := c.operationArguments(arguments)
	if err != nil {
		return err
	}
	return c.dispatch(ctx, op, in, out, true)
}

func (c *Client) operationArguments(arguments map[string]any) (map[string]any, error) {
	if len(arguments) > 10000 {
		return nil, fmt.Errorf("too many operation arguments")
	}
	in := make(map[string]any, len(arguments)+3)
	for key, value := range arguments {
		in[key] = value
	}
	for key, value := range map[string]string{"org": c.identity.Org, "namespace": c.identity.Namespace, "agent": c.identity.Agent} {
		if _, supplied := in[key]; !supplied && value != "" {
			in[key] = value
		}
	}
	return in, nil
}

func validOperation(op string) bool {
	if len(op) == 0 || len(op) > 160 {
		return false
	}
	for _, ch := range op {
		if ch != '.' && ch != '_' && (ch < 'a' || ch > 'z') && (ch < '0' || ch > '9') {
			return false
		}
	}
	if op[0] == '.' || op[len(op)-1] == '.' {
		return false
	}
	for i := 1; i < len(op); i++ {
		if op[i] == '.' && op[i-1] == '.' {
			return false
		}
	}
	return true
}
