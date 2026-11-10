package state

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestSaveAndLoadToken(t *testing.T) {
	dir := t.TempDir()
	path, err := SaveToken(dir, Token{Server: "https://grid.example", Device: "dev1", Token: "t0ken"})
	if err != nil {
		t.Fatal(err)
	}
	if path != filepath.Join(dir, "token.json") {
		t.Fatalf("token path is %s", path)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("token is %o, not 0600", info.Mode().Perm())
		}
	}
	loaded, err := LoadToken(dir, "https://grid.example/")
	if err != nil || loaded != "t0ken" {
		t.Fatalf("loaded %q (%v)", loaded, err)
	}
	if _, err := LoadToken(dir, "https://other.example"); err == nil {
		t.Fatal("a token for another server was accepted")
	}
}

func TestExpandHome(t *testing.T) {
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home directory")
	}
	if got := ExpandHome("~/GridWorkspace"); got != filepath.Join(home, "GridWorkspace") {
		t.Fatalf("ExpandHome gave %q", got)
	}
	if got := ExpandHome("/absolute/path"); got != "/absolute/path" {
		t.Fatalf("ExpandHome changed an absolute path: %q", got)
	}
}

func TestToolAndRuntimePaths(t *testing.T) {
	dir := "/state"
	if got := ToolDir(dir, "abc123"); got != filepath.Join("/state", "tools", "abc123") {
		t.Fatalf("ToolDir gave %s", got)
	}
	if got := PythonRuntimeDir(dir); got != filepath.Join("/state", "runtime", "python") {
		t.Fatalf("PythonRuntimeDir gave %s", got)
	}
}
