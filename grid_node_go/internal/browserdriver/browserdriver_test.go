package browserdriver

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseKeyCombinations(t *testing.T) {
	info := parseKey("Control+Shift+A")
	if info.key != "A" || info.vk != int('A') {
		t.Fatalf("key is %+v", info)
	}
	if info.modifiers != 2|8 {
		t.Fatalf("modifiers are %d", info.modifiers)
	}
	enter := parseKey("Enter")
	if enter.key != "Enter" || enter.vk != 13 {
		t.Fatalf("Enter is %+v", enter)
	}
	space := parseKey("Space")
	if space.key != " " || space.vk != 32 {
		t.Fatalf("Space is %+v", space)
	}
	upper := parseKey("Control+a")
	if upper.modifiers != 2 || upper.key != "a" {
		t.Fatalf("Control+a is %+v", upper)
	}
}

func TestFindExecutableUsesTheConfiguredBrowser(t *testing.T) {
	dir := t.TempDir()
	binary := filepath.Join(dir, "chrome")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	found, err := findExecutable(binary)
	if err != nil || found != binary {
		t.Fatalf("findExecutable gave %q (%v)", found, err)
	}
	if _, err := findExecutable(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("a missing configured browser was accepted")
	}
}

func TestNewUsesTheStateDirProfile(t *testing.T) {
	driver := New("/state", "", true)
	if driver.Profile != filepath.Join("/state", "profile") {
		t.Fatalf("profile is %s", driver.Profile)
	}
	if !driver.Headless {
		t.Fatal("headless was lost")
	}
}

func TestToolNamesAndSchemas(t *testing.T) {
	driver := New(t.TempDir(), "", true)
	wanted := map[string]bool{
		"browser_navigate": false, "browser_snapshot": false, "browser_screenshot": false,
		"browser_click": false, "browser_type": false, "browser_press": false,
		"browser_scroll": false, "browser_history": false, "browser_tabs": false, "browser_wait": false,
	}
	for _, tool := range driver.Tools() {
		if _, ok := wanted[tool.Name]; !ok {
			t.Fatalf("unexpected tool %s", tool.Name)
		}
		wanted[tool.Name] = true
		if tool.InputSchema == nil || tool.Handler == nil {
			t.Fatalf("%s is incomplete", tool.Name)
		}
	}
	for name, seen := range wanted {
		if !seen {
			t.Fatalf("missing tool %s", name)
		}
	}
}

func TestSnapshotScriptAndQuoting(t *testing.T) {
	if !strings.Contains(snapshotJS, "data-grid-ref") {
		t.Fatal("the snapshot script assigns no refs")
	}
	if got := jsQuote(`a "b"` + "\n"); got != `"a \"b\"\n"` {
		t.Fatalf("jsQuote gave %s", got)
	}
}

func TestCapabilities(t *testing.T) {
	capabilities := Capabilities()
	if len(capabilities) != 2 || capabilities[0] != "browser" || capabilities[1] != "screen" {
		t.Fatalf("capabilities are %v", capabilities)
	}
}
