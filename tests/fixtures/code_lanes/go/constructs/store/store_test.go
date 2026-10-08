// Every Go form the fast tier reads (synthetic).
package store

import (
	"context"
	"testing"
)

func TestStore_Get(t *testing.T) {
	s := NewStore("t")
	cases := []struct{ key string }{{"a"}, {"b"}}
	for _, c := range cases {
		t.Run(c.key, func(t *testing.T) {
			if _, err := s.Get(context.Background(), c.key); err == nil {
				t.Fatal("expected miss")
			}
		})
	}
}

func BenchmarkStore_Has(b *testing.B) {
	s := NewStore("b")
	for i := 0; i < b.N; i++ {
		s.Has("x")
	}
}

func ExampleNewStore() {
	_ = NewStore("x")
}

func FuzzPut(f *testing.F) {
	f.Fuzz(func(t *testing.T, k string) { _ = NewStore("f").Put(context.Background(), k, nil) })
}
