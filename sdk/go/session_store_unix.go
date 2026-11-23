//go:build linux || darwin || freebsd || netbsd || openbsd || dragonfly

package loom

import (
	"context"
	"fmt"
	"os"
	"syscall"
	"time"
)

func sessionPlatformSupported() bool { return true }

func sessionFileIdentity(info os.FileInfo) (string, error) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", ErrSessionProtectionUnavailable
	}
	return fmt.Sprintf("%d:%d", stat.Dev, stat.Ino), nil
}

func sessionPrivate(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid()) && info.Mode()&os.ModeSymlink == 0 && info.Mode().Perm()&0077 == 0
}

func sessionLock(ctx context.Context, file *os.File) error {
	ticker := time.NewTicker(10*time.Millisecond)
	defer ticker.Stop()
	for {
		err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return nil
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN && err != syscall.EINTR {
			return ErrSessionProtectionUnavailable
		}
		select {
		case <-ctx.Done():
			return ErrSessionProtectionUnavailable
		case <-ticker.C:
		}
	}
}

func sessionUnlock(file *os.File) { _ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN) }
