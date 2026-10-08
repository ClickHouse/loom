// Every Go form the fast tier reads (synthetic).
package main

import (
	"context"
	"fmt"
	"os"
	"sync"

	st "example.com/constructs/store"
)

//go:generate stringer -type=Mode

// Mode is a mode.
type Mode int

type app struct {
	store *st.Store
	mu    sync.Mutex
}

func newApp(s *st.Store) *app { return &app{store: s} }

func (a *app) run(ctx context.Context) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.store.Has("k") {
		return a.store.Put(ctx, "k", []byte("v"))
	}
	v, err := a.store.Get(ctx, "k")
	if err != nil {
		return err
	}
	fmt.Println(string(v), a.store.Describe())
	var rw st.ReadWriter = a.store
	_ = rw
	get := a.store.Get
	_, _ = get(ctx, "k")
	go a.background(ctx)
	return nil
}

func (a *app) background(ctx context.Context) {
	s := st.NewStore("bg")
	_ = s.Has("z")
	p := st.Pair[string, int]{Key: "a", Val: 1}
	_ = p.First()
	_ = st.Map([]int{1}, func(i int) string { return fmt.Sprint(i) })
	var r st.Reader = s
	_, _ = r.Get(ctx, "z")
}

func main() {
	s := st.NewStore("main")
	if err := newApp(s).run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
