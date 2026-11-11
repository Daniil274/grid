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
// The same restriction hits every program this node wants to run: bash, the
// portable runtime of a tool package, a newer self. Command answers it the same
// way Termux's own exec hook does - through the system linker, with the
// libtermux-exec.so preload that makes the program's own exec calls take the
// same detour.
//
// On every other system this package changes nothing: the environment variable
// is not set, argv[0] is not a linker, and Command is os/exec.Command.
package android

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
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

// Command starts a program this node runs on purpose: exec.Command, with
// Android's exec restriction answered. A binary kept in an app's private
// storage may not be exec'd there, so it is started by the system linker
// exactly the way Termux's exec hook starts everything - and the way this
// node itself was started.
func Command(program string, args ...string) *exec.Cmd {
	return CommandContext(nil, program, args...)
}

// CommandContext is Command with a context, as os/exec.CommandContext.
func CommandContext(ctx context.Context, program string, args ...string) *exec.Cmd {
	linker, execLib := "", ""
	if runtime.GOOS == "android" {
		linker = systemLinker()
		if !exists(linker) {
			linker = ""
		}
		execLib = termuxExecLib()
	}
	path, argv, env := plan(linker, program, args, os.Environ(), execLib)
	var command *exec.Cmd
	if ctx != nil {
		command = exec.CommandContext(ctx, path, argv...)
	} else {
		command = exec.Command(path, argv...)
	}
	if env != nil {
		command.Env = env
	}
	return command
}

// systemLinker names the loader that runs a private-storage binary.
func systemLinker() string {
	if runtime.GOARCH == "arm" || runtime.GOARCH == "386" {
		return "/system/bin/linker"
	}
	return "/system/bin/linker64"
}

// termuxExecLib is the library Termux preloads into its programs so their own
// exec calls take the same detour, when there is one.
func termuxExecLib() string {
	prefix := os.Getenv("PREFIX")
	if prefix == "" {
		prefix = "/data/data/com.termux/files/usr"
	}
	lib := filepath.Join(prefix, "lib", "libtermux-exec.so")
	if exists(lib) {
		return lib
	}
	return ""
}

// plan decides how program is started: through the linker when one is known
// and the program needs it - an absolute path outside the system partitions,
// the only kind Android refuses to run directly. The linker takes the program
// as its first argument and hands it the rest (bionic's linker_main reads
// exe_to_load from argv[1]); a program the linker starts sees a plain
// command line again. Plainly otherwise, with a nil env so the caller's
// environment passes through as is.
//
// The returned path is what exec.Command is given; argv is the arguments that
// follow it.
func plan(linker, program string, args []string, environ []string, execLib string) (path string, argv []string, env []string) {
	if linker == "" || !filepath.IsAbs(program) || isSystemPath(program) {
		return program, args, nil
	}
	argv = append([]string{program}, args...)
	env = withVar(environ, SelfEnv, program)
	if execLib != "" {
		env = withPreload(env, execLib)
	}
	return linker, argv, env
}

// isSystemPath reports whether Android runs this file without the linker.
func isSystemPath(program string) bool {
	for _, root := range []string{"/apex/", "/odm/", "/product/", "/sbin/", "/system/", "/system_ext/", "/vendor/"} {
		if strings.HasPrefix(program, root) {
			return true
		}
	}
	return false
}

// withVar returns a copy of environ with key=value in place of any older
// value of that key.
func withVar(environ []string, key, value string) []string {
	updated := make([]string, 0, len(environ)+1)
	for _, entry := range environ {
		if !strings.HasPrefix(entry, key+"=") {
			updated = append(updated, entry)
		}
	}
	return append(updated, key+"="+value)
}

// withPreload makes sure libtermux-exec.so rides along in LD_PRELOAD, so the
// program the linker starts runs its own children the same way.
func withPreload(environ []string, lib string) []string {
	for _, entry := range environ {
		if !strings.HasPrefix(entry, "LD_PRELOAD=") {
			continue
		}
		value := strings.TrimPrefix(entry, "LD_PRELOAD=")
		for _, item := range strings.FieldsFunc(value, func(r rune) bool { return r == ':' || r == ' ' }) {
			if item == lib {
				return environ
			}
		}
		replacement := "LD_PRELOAD=" + lib
		if strings.TrimSpace(value) != "" {
			replacement += ":" + value
		}
		updated := make([]string, 0, len(environ))
		for _, other := range environ {
			if other != entry {
				updated = append(updated, other)
			}
		}
		return append(updated, replacement)
	}
	return append(environ, "LD_PRELOAD="+lib)
}

// exists reports whether path is there at all.
func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
