//go:build !linux && !darwin && !freebsd && !netbsd && !openbsd && !dragonfly

package loom

import (
	"context"
	"os"
)

func sessionPlatformSupported() bool { return false }
func sessionFileIdentity(os.FileInfo) (string, error) { return "", ErrSessionProtectionUnavailable }
func sessionPrivate(os.FileInfo) bool { return false }
func sessionLock(context.Context, *os.File) error { return ErrSessionProtectionUnavailable }
func sessionUnlock(*os.File) {}
