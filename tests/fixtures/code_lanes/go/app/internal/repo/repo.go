// Package repo holds the store (synthetic).
package repo

import "database/sql"

type Store struct{ db *sql.DB }

func (s *Store) Count() (int, error) {
	var n int
	err := s.db.QueryRow("SELECT count(*) FROM users WHERE active = 1").Scan(&n)
	return n, err
}
