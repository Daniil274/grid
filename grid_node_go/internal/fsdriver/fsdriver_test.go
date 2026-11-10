package fsdriver

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gridnode/internal/protocol"
)

func newDriver(t *testing.T) (*Driver, string) {
	t.Helper()
	root := t.TempDir()
	workdir := filepath.Join(root, "work")
	if err := os.MkdirAll(workdir, 0o755); err != nil {
		t.Fatal(err)
	}
	return New(workdir), workdir
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func textOf(t *testing.T, content []protocol.Content) string {
	t.Helper()
	if len(content) == 0 {
		t.Fatal("no content")
	}
	return content[0].Text
}

func TestResolveKeepsPathsInsideWorkdir(t *testing.T) {
	driver, workdir := newDriver(t)
	outside := filepath.Join(filepath.Dir(workdir), "secret.txt")
	writeFile(t, outside, "secret")

	if _, err := driver.Resolve(map[string]any{}, "../secret.txt"); err == nil {
		t.Fatal("a path above the workdir was allowed")
	} else if !strings.Contains(err.Error(), "is outside working directory") {
		t.Fatalf("unexpected error: %v", err)
	}
	inside, err := driver.Resolve(map[string]any{}, "notes/a.txt")
	if err != nil {
		t.Fatalf("a path inside the workdir was refused: %v", err)
	}
	if !strings.HasPrefix(inside, workdir) {
		t.Fatalf("%s is not inside %s", inside, workdir)
	}
	external, err := driver.Resolve(map[string]any{"allow_external": true}, outside)
	if err != nil {
		t.Fatalf("allow_external was ignored: %v", err)
	}
	if external != outside {
		t.Fatalf("external resolved to %s, not %s", external, outside)
	}
}

func TestResolveRefusesSymlinkEscape(t *testing.T) {
	driver, workdir := newDriver(t)
	outside := filepath.Join(filepath.Dir(workdir), "outside")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(workdir, "link")
	if err := os.Symlink(outside, link); err != nil {
		t.Skip("symlinks are not available: ", err)
	}
	if _, err := driver.Resolve(map[string]any{}, "link/secret.txt"); err == nil {
		t.Fatal("a symlink out of the workdir was followed")
	}
}

func TestReadOffsetAndLimit(t *testing.T) {
	driver, workdir := newDriver(t)
	writeFile(t, filepath.Join(workdir, "lines.txt"), "one\ntwo\nthree\nfour\nfive\n")
	content, err := driver.Read(map[string]any{"filepath": "lines.txt", "offset": 1, "limit_lines": 2})
	if err != nil {
		t.Fatal(err)
	}
	if got := textOf(t, content); got != "two\nthree" {
		t.Fatalf("read %q", got)
	}
	all, err := driver.Read(map[string]any{"filepath": "lines.txt"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(textOf(t, all), "five") {
		t.Fatalf("all lines were not read: %q", textOf(t, all))
	}
}

func TestReadImageReturnsImageContent(t *testing.T) {
	driver, workdir := newDriver(t)
	pixel := []byte{0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n', 0, 0, 0, 13}
	writeFile(t, filepath.Join(workdir, "pixel.png"), string(pixel))
	content, err := driver.Read(map[string]any{"filepath": "pixel.png"})
	if err != nil {
		t.Fatal(err)
	}
	if len(content) != 2 || content[1].Type != "image" || content[1].MimeType != "image/png" || content[1].Data == "" {
		t.Fatalf("unexpected content: %+v", content)
	}
}

func TestWriteRefusesOverwrite(t *testing.T) {
	driver, workdir := newDriver(t)
	path := filepath.Join(workdir, "a.txt")
	if _, err := driver.Write(map[string]any{"filepath": "a.txt", "content": "one"}); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.Write(map[string]any{"filepath": "a.txt", "content": "two"}); err == nil {
		t.Fatal("an existing file was replaced without overwrite")
	}
	if _, err := driver.Write(map[string]any{"filepath": "a.txt", "content": "two", "overwrite": true}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "two" {
		t.Fatalf("file holds %q (%v)", data, err)
	}
}

func TestAppendAddsNewline(t *testing.T) {
	driver, workdir := newDriver(t)
	path := filepath.Join(workdir, "log.txt")
	writeFile(t, path, "first")
	if _, err := driver.Append(map[string]any{"filepath": "log.txt", "content": "second"}); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if string(data) != "first\nsecond" {
		t.Fatalf("append wrote %q", data)
	}
	if _, err := driver.Append(map[string]any{"filepath": "fresh.txt", "content": "only"}); err != nil {
		t.Fatal(err)
	}
	fresh, _ := os.ReadFile(filepath.Join(workdir, "fresh.txt"))
	if string(fresh) != "only" {
		t.Fatalf("append to a new file wrote %q", fresh)
	}
}

func TestEditAppliesSeveralHunks(t *testing.T) {
	driver, workdir := newDriver(t)
	original := "alpha\nbeta\ngamma\ndelta\nepsilon\n"
	writeFile(t, filepath.Join(workdir, "code.txt"), original)
	patch := `--- a/code.txt
+++ b/code.txt
@@ -1,2 +1,2 @@
 alpha
-beta
+BETA
@@ -4,2 +4,2 @@
 delta
-epsilon
+EPSILON
`
	content, err := driver.Edit(map[string]any{"filepath": "code.txt", "patch_content": patch})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(textOf(t, content), "2 hunk") {
		t.Fatalf("unexpected answer: %q", textOf(t, content))
	}
	data, _ := os.ReadFile(filepath.Join(workdir, "code.txt"))
	if string(data) != "alpha\nBETA\ngamma\ndelta\nEPSILON\n" {
		t.Fatalf("patched file is %q", data)
	}
}

func TestApplyUnifiedDiffToleratesOffset(t *testing.T) {
	original := "header\none\ntwo\nthree\n"
	patch := `@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
`
	updated, hunks, err := ApplyUnifiedDiff(original, patch)
	if err != nil {
		t.Fatal(err)
	}
	if hunks != 1 || !strings.Contains(updated, "TWO") || !strings.Contains(updated, "header") {
		t.Fatalf("diff applied to %q", updated)
	}
}

func TestApplyUnifiedDiffFailsWhenNothingMatches(t *testing.T) {
	if _, _, err := ApplyUnifiedDiff("alpha\n", "@@ -1 +1 @@\n-beta\n+BETA\n"); err == nil {
		t.Fatal("a patch that matches nothing was applied")
	}
}

func TestGlobFindsRecursiveMatches(t *testing.T) {
	driver, workdir := newDriver(t)
	writeFile(t, filepath.Join(workdir, "top.go"), "package top")
	writeFile(t, filepath.Join(workdir, "pkg", "deep", "inner.go"), "package inner")
	writeFile(t, filepath.Join(workdir, "notes.txt"), "hello")
	content, err := driver.Glob(map[string]any{"pattern": "**/*.go"})
	if err != nil {
		t.Fatal(err)
	}
	body := textOf(t, content)
	if !strings.Contains(body, "top.go") || !strings.Contains(body, "pkg/deep/inner.go") {
		t.Fatalf("glob found %q", body)
	}
	if strings.Contains(body, "notes.txt") {
		t.Fatalf("glob matched a txt file: %q", body)
	}
	limited, err := driver.Glob(map[string]any{"pattern": "**/*", "max_results": 1})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(textOf(t, limited), "limit reached") {
		t.Fatalf("max_results ignored: %q", textOf(t, limited))
	}
}

func TestGrepFindsAndFilters(t *testing.T) {
	driver, workdir := newDriver(t)
	writeFile(t, filepath.Join(workdir, "a.go"), "package a\n// TODO: one\n")
	writeFile(t, filepath.Join(workdir, "b.py"), "x = 1  # TODO: two\n")
	writeFile(t, filepath.Join(workdir, "c.txt"), "TODO: three\n")
	content, err := driver.Grep(map[string]any{"pattern": "TODO", "file_extensions": "go"})
	if err != nil {
		t.Fatal(err)
	}
	body := textOf(t, content)
	if !strings.Contains(body, "a.go:2: // TODO: one") {
		t.Fatalf("grep answered %q", body)
	}
	if strings.Contains(body, "b.py") || strings.Contains(body, "c.txt") {
		t.Fatalf("the extension filter was ignored: %q", body)
	}
	regex, err := driver.Grep(map[string]any{"pattern": "^TODO", "use_regex": true, "case_sensitive": true})
	if err != nil {
		t.Fatal(err)
	}
	regexBody := textOf(t, regex)
	if !strings.Contains(regexBody, "c.txt") || strings.Contains(regexBody, "b.py") {
		t.Fatalf("regex grep answered %q", regexBody)
	}
}

func TestListDirsCreatesAndLists(t *testing.T) {
	driver, workdir := newDriver(t)
	if err := os.MkdirAll(filepath.Join(workdir, "child"), 0o755); err != nil {
		t.Fatal(err)
	}
	created, err := driver.ListDirs(map[string]any{"mkdir": "created"})
	if err != nil {
		t.Fatal(err)
	}
	var inside Listing
	if err := json.Unmarshal([]byte(textOf(t, created)), &inside); err != nil {
		t.Fatal(err)
	}
	if inside.Current != filepath.Join(workdir, "created") || len(inside.Dirs) != 0 {
		t.Fatalf("mkdir did not list the new folder: %+v", inside)
	}
	content, err := driver.ListDirs(map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	var listing Listing
	if err := json.Unmarshal([]byte(textOf(t, content)), &listing); err != nil {
		t.Fatal(err)
	}
	if listing.Platform == "" || listing.Current != workdir || listing.DefaultWorkdir != workdir {
		t.Fatalf("unexpected listing: %+v", listing)
	}
	names := map[string]bool{}
	for _, item := range listing.Dirs {
		names[item.Name] = true
	}
	if !names["child"] || !names["created"] {
		t.Fatalf("listing has no folders: %+v", listing.Dirs)
	}
	if len(listing.Roots) == 0 {
		t.Fatal("no roots for the picker")
	}
	if _, err := os.Stat(filepath.Join(workdir, "created")); err != nil {
		t.Fatalf("mkdir did not create the folder: %v", err)
	}
}

func TestToolsListHasAnnotations(t *testing.T) {
	driver, _ := newDriver(t)
	names := map[string]bool{}
	for _, tool := range driver.Tools() {
		names[tool.Name] = true
		if tool.InputSchema == nil {
			t.Fatalf("%s has no input schema", tool.Name)
		}
	}
	for _, expected := range []string{"fs_read", "fs_write", "fs_edit", "fs_append", "fs_glob", "fs_grep", "fs_list_dirs"} {
		if !names[expected] {
			t.Fatalf("missing tool %s", expected)
		}
	}
}

var _ = context.Background
