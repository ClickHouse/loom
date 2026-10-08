// The service's entry point (synthetic): one import from
// its own module, one from a sibling module named by a replace directive.
package main

import (
	"fmt"

	"example.com/mono/lib/store"
	"example.com/mono/svc/internal/handler"
)

func main() {
	fmt.Println(handler.Serve(store.Open("dsn")))
}
