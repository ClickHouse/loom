package main

import (
	"fmt"

	"example.com/app/internal/repo"
)

func main() {
	var s repo.Store
	n, _ := s.Count()
	fmt.Println(n)
}
