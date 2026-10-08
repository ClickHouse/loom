// Every Go form the fast tier reads (synthetic).
// Package store keeps things.
package store

import (
	"context"
	"fmt"
	"io"
)

// Version is the store's version.
const Version = "1.0"

const (
	KindA Kind = iota
	KindB
	KindC
)

var (
	// ErrMissing is returned when a key is absent.
	ErrMissing = fmt.Errorf("missing")
	handlers   = map[string]Handler{}
)

var defaultStore = NewStore("mem")

// Kind is an enum-like type.
type Kind int

// Handler handles a request.
type Handler func(ctx context.Context, key string) error

// Reader reads keys.
type Reader interface {
	Get(ctx context.Context, key string) ([]byte, error)
	Has(key string) bool
}

// Writer writes keys.
type Writer interface {
	Put(ctx context.Context, key string, v []byte) error
}

// ReadWriter is both.
type ReadWriter interface {
	Reader
	Writer
	io.Closer
}

// Base carries an id.
type Base struct {
	ID string
}

// Describe describes the base.
func (b Base) Describe() string { return b.ID }

// Store is a memory store.
type Store struct {
	Base
	name string
	data map[string][]byte
}

// Alias is an alias.
type Alias = Store

// NewStore builds a store.
func NewStore(name string) *Store {
	return &Store{Base: Base{ID: name}, name: name, data: map[string][]byte{}}
}

// Get reads a key.
func (s *Store) Get(ctx context.Context, key string) ([]byte, error) {
	if v, ok := s.data[key]; ok {
		return v, nil
	}
	return nil, ErrMissing
}

// Has says whether a key exists.
func (s *Store) Has(key string) bool { _, ok := s.data[key]; return ok }

// Put writes a key.
func (s *Store) Put(ctx context.Context, key string, v []byte) error {
	s.data[key] = v
	return nil
}

// Close closes.
func (s *Store) Close() error { return nil }

// Pair is a generic pair.
type Pair[K comparable, V any] struct {
	Key K
	Val V
}

// First returns the key.
func (p Pair[K, V]) First() K { return p.Key }

// Map maps a slice.
func Map[T, U any](xs []T, f func(T) U) []U {
	out := make([]U, 0, len(xs))
	for _, x := range xs {
		out = append(out, f(x))
	}
	return out
}

func init() {
	handlers["a"] = func(ctx context.Context, key string) error { return nil }
}

func init() {
	_ = defaultStore.Describe()
}
