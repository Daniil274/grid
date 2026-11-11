// Package android carries what Android does to a program Termux starts.
//
// Android 10+ refuses to exec a file in an app's private directory, so Termux
// runs such a program through the system linker:
//
//	/system/bin/linker64 /data/data/com.termux/files/home/.grid-node/bin/grid-node --server ...
//
// bionic takes that extra path away for the programs it links itself, but a Go
// program starts from the raw stack and still sees it, and /proc/self/exe names
// the linker rather than the program. Both are answered here, so the rest of
// the node sees what it would see on Linux.
//
// On every other system this package changes nothing: the environment variable
// is not set and argv[0] is not a linker.
package android

import (
	"os"
	"path/filepath"
)

// SelfEnv is where termux-exec leaves the path of the program it started.
const SelfEnv = "TERMUX_EXEC__PROC_SELF_EXE"

// linkers are the names the system loader is started under.
var linkers = map[string]bool{"linker64": true, "linker": true}

// Self is the path of the running program, also when the linker started it.
func Self() string {
	if self := os.Getenv(SelfEnv); self != "" {
		return self
	}
	path, err := os.Executable()
	if err != nil {
		return ""
	}
	return path
}

// Args is argv without the program's own path that the linker leaves in it.
//
// The first element stays: it names this process, and the linker's name is as
// good a name as the program's.
func Args(argv []string) []string {
	if len(argv) < 2 {
		return argv
	}
	self := os.Getenv(SelfEnv)
	if self == "" {
		// An older termux-exec names nothing, but then the linker itself is the
		// first element and the program it started follows it.
		if !linkers[filepath.Base(argv[0])] {
			return argv
		}
		self = argv[1]
		if !filepath.IsAbs(self) {
			return argv
		}
		if _, err := os.Stat(self); err != nil {
			return argv
		}
	}
	if argv[1] != self {
		return argv
	}
	trimmed := make([]string, 0, len(argv)-1)
	trimmed = append(trimmed, self)
	return append(trimmed, argv[2:]...)
}
