package android

import (
	"os"
	"path/filepath"
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
