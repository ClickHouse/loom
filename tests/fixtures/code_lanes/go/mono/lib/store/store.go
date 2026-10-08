// Package store opens the shared store (synthetic: a
// module of a multi-module repository, imported across modules).
package store

// Open returns a handle.
func Open(dsn string) string {
	return dsn
}
