package android

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestArgsDropsThePathTheLinkerLeaves(t *testing.T) {
	self := filepath.Join(t.TempDir(), "grid-node")
	if err := os.WriteFile(self, []byte("bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(SelfEnv, self)

	// What Go sees under Termux: the linker, the program, then its arguments.
	got := Args([]string{"/system/bin/linker64", self, "--server", "https://grid.example"})
	want := []string{self, "--server", "https://grid.example"}
	if len(got) != len(want) {
		t.Fatalf("args are %q, want %q", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("args are %q, want %q", got, want)
		}
	}
}

func TestArgsKeepsAnOrdinaryCommandLine(t *testing.T) {
	t.Setenv(SelfEnv, "/data/data/com.termux/files/home/.grid-node/bin/grid-node")
	for _, argv := range [][]string{
		{"/data/data/com.termux/files/home/.grid-node/bin/grid-node", "version"},
		{"/data/data/com.termux/files/home/.grid-node/bin/grid-node", "--server", "x"},
		{"grid-node"},
	} {
		if got := Args(argv); len(got) != len(argv) {
			t.Fatalf("args %q became %q", argv, got)
		}
	}
}

func TestArgsWithoutTermuxEnvironmentNamesTheLinker(t *testing.T) {
	dir := t.TempDir()
	self := filepath.Join(dir, "grid-node")
	if err := os.WriteFile(self, []byte("bin"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(SelfEnv, "")

	got := Args([]string{"/system/bin/linker64", self, "version"})
	if len(got) != 2 || got[0] != self || got[1] != "version" {
		t.Fatalf("args are %q", got)
	}
	// Not a linker, nothing trimmed.
	plain := []string{"/usr/bin/grid-node", "/etc/hosts"}
	if got := Args(plain); len(got) != 2 {
		t.Fatalf("args %q became %q", plain, got)
	}
}

func TestSelfFallsBackToTheEnvironment(t *testing.T) {
	t.Setenv(SelfEnv, "/data/data/com.termux/files/home/.grid-node/bin/grid-node")
	if got := Self(); got != "/data/data/com.termux/files/home/.grid-node/bin/grid-node" {
		t.Fatalf("self is %q", got)
	}
}

func TestPlanRunsPrivateStorageThroughTheLinker(t *testing.T) {
	bash := "/data/data/com.termux/files/usr/bin/bash"
	environ := []string{
		"PATH=/data/data/com.termux/files/usr/bin",
		SelfEnv + "=/data/data/com.termux/files/home/.grid-node/bin/grid-node",
		"LD_PRELOAD=/data/data/com.termux/files/usr/lib/libtermux-exec.so",
	}
	path, argv, env := plan("/system/bin/linker64", bash, []string{"-c", "echo hi"}, environ,
		"/data/data/com.termux/files/usr/lib/libtermux-exec.so")

	if path != "/system/bin/linker64" {
		t.Fatalf("path is %q, want the linker", path)
	}
	wantArgv := []string{bash, "-c", "echo hi"}
	if len(argv) != len(wantArgv) {
		t.Fatalf("argv is %q, want %q", argv, wantArgv)
	}
	for i := range wantArgv {
		if argv[i] != wantArgv[i] {
			t.Fatalf("argv is %q, want %q", argv, wantArgv)
		}
	}
	var self, preload string
	for _, entry := range env {
		switch {
		case strings.HasPrefix(entry, SelfEnv+"="):
			self = strings.TrimPrefix(entry, SelfEnv+"=")
		case strings.HasPrefix(entry, "LD_PRELOAD="):
			preload = strings.TrimPrefix(entry, "LD_PRELOAD=")
		}
	}
	if self != bash {
		t.Fatalf("self env is %q, want %q", self, bash)
	}
	if !strings.Contains(preload, "libtermux-exec.so") {
		t.Fatalf("LD_PRELOAD is %q, want the termux exec hook kept", preload)
	}
}

func TestPlanRunsSystemAndPlainProgramsDirectly(t *testing.T) {
	// Android runs these as is: system files, and anything not absolute.
	for _, program := range []string{"/system/bin/sh", "bash"} {
		path, argv, env := plan("/system/bin/linker64", program, []string{"-c"}, nil, "")
		if path != program || env != nil {
			t.Fatalf("program %q runs as %q with env %v, want itself and no env", program, path, env)
		}
		if len(argv) != 1 || argv[0] != "-c" {
			t.Fatalf("program %q got argv %q", program, argv)
		}
	}
	// Without a linker nothing can take the detour.
	path, _, env := plan("", "/data/data/com.termux/files/usr/bin/bash", nil, nil, "")
	if path != "/data/data/com.termux/files/usr/bin/bash" || env != nil {
		t.Fatalf("a missing linker must fall back to a plain exec, got %q env %v", path, env)
	}
}

func TestWithPreloadKeepsTheHookOnce(t *testing.T) {
	lib := "/data/data/com.termux/files/usr/lib/libtermux-exec.so"
	env := []string{"LD_PRELOAD=" + lib}
	if got := withPreload(env, lib); len(got) != 1 || got[0] != env[0] {
		t.Fatalf("LD_PRELOAD %q grew to %q", env[0], got)
	}
	env = []string{"LD_PRELOAD=/other/lib.so"}
	got := withPreload(env, lib)
	if len(got) != 1 || !strings.HasPrefix(got[0], "LD_PRELOAD="+lib+":") {
		t.Fatalf("LD_PRELOAD %q became %q", env[0], got)
	}
	got = withPreload([]string{"HOME=/root"}, lib)
	if len(got) != 2 || got[1] != "LD_PRELOAD="+lib {
		t.Fatalf("a missing LD_PRELOAD must appear, got %q", got)
	}
}
