package toolrunner

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"

	"gridnode/internal/protocol"
)

// archive builds a tar.gz of the given files.
func archive(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var buffer bytes.Buffer
	gzipWriter := gzip.NewWriter(&buffer)
	writer := tar.NewWriter(gzipWriter)
	for name, content := range files {
		header := &tar.Header{Name: name, Mode: 0o755, Size: int64(len(content)), Typeflag: tar.TypeReg}
		if err := writer.WriteHeader(header); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gzipWriter.Close(); err != nil {
		t.Fatal(err)
	}
	return buffer.Bytes()
}

func TestExtractTarGzRefusesEscapingEntries(t *testing.T) {
	payload := archive(t, map[string]string{"../evil.txt": "x"})
	if err := ExtractTarGz(bytes.NewReader(payload), t.TempDir()); err == nil {
		t.Fatal("a path traversal entry was unpacked")
	}
}

func TestExtractTarGzUnpacksFiles(t *testing.T) {
	dest := t.TempDir()
	payload := archive(t, map[string]string{"pkg/hello.txt": "hi", "tool.json": `{"name":"x"}`})
	if err := ExtractTarGz(bytes.NewReader(payload), dest); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(dest, "pkg", "hello.txt"))
	if err != nil || string(data) != "hi" {
		t.Fatalf("unpacked %q (%v)", data, err)
	}
}

func TestExecDownloadsAndRunsAToolPackage(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the test package runs sh")
	}
	payload := archive(t, map[string]string{
		"tool.json": `{"command":["/bin/sh","-c","cat > /dev/null; echo tool-ok"]}`,
	})
	var requests int64
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		atomic.AddInt64(&requests, 1)
		if request.URL.Path != "/node/tools/words/abc123.tar.gz" {
			http.NotFound(writer, request)
			return
		}
		if request.Header.Get("Authorization") != "Bearer secret" {
			writer.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = writer.Write(payload)
	}))
	defer server.Close()

	stateDir := t.TempDir()
	runner := New(stateDir, server.URL, "secret", t.TempDir())
	args := map[string]any{"package": "words", "hash": "abc123", "fn": "count", "arguments": map[string]any{"path": "x"}}
	content, err := runner.Exec(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content[0].Text, "tool-ok") {
		t.Fatalf("tool answered %q", content[0].Text)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "tools", "abc123", "tool.json")); err != nil {
		t.Fatalf("the package was not cached: %v", err)
	}
	if _, err := runner.Exec(context.Background(), args); err != nil {
		t.Fatal(err)
	}
	if atomic.LoadInt64(&requests) != 1 {
		t.Fatalf("the package was downloaded %d times", requests)
	}
}

func TestExecRefusesABadHash(t *testing.T) {
	runner := New(t.TempDir(), "", "", t.TempDir())
	if _, err := runner.Exec(context.Background(), map[string]any{"package": "x", "hash": "../escape", "fn": "f"}); err == nil {
		t.Fatal("a hash with a path separator was accepted")
	}
}

func TestExecSaysWhenItCannotDownload(t *testing.T) {
	runner := New(t.TempDir(), "", "", t.TempDir())
	_, err := runner.Exec(context.Background(), map[string]any{"package": "x", "hash": "deadbeef", "fn": "f"})
	if err == nil || !strings.Contains(err.Error(), "not cached") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestExecRunsAPythonEntry(t *testing.T) {
	python := ""
	for _, name := range []string{"python3", "python"} {
		if path, err := exec.LookPath(name); err == nil {
			python = path
			break
		}
	}
	if python == "" {
		t.Skip("no Python interpreter")
	}
	script := "import json, sys\n" +
		"args = json.loads(sys.stdin.read() or '{}')\n" +
		"print(json.dumps({'fn': sys.argv[1], 'got': args}))\n"
	payload := archive(t, map[string]string{
		"tool.json": `{"runtime":"python","entry":"run.py"}`,
		"run.py":    script,
	})
	var requests int64
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		atomic.AddInt64(&requests, 1)
		_, _ = writer.Write(payload)
	}))
	defer server.Close()
	runner := New(t.TempDir(), server.URL, "tok", t.TempDir())
	content, err := runner.Exec(context.Background(), map[string]any{
		"package": "pykit", "hash": "cafe01", "fn": "hello", "arguments": map[string]any{"x": 1},
	})
	if err != nil {
		t.Fatal(err)
	}
	var answer map[string]any
	if err := json.Unmarshal([]byte(content[0].Text), &answer); err != nil {
		t.Fatalf("python output %q is not JSON: %v", content[0].Text, err)
	}
	if answer["fn"] != "hello" {
		t.Fatalf("python answered %v", answer)
	}
}

func TestDetectPackage(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "words"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "words", "__init__.py"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "grid_tool.py"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := detectPackage(dir); got != "words" {
		t.Fatalf("detectPackage gave %q", got)
	}
}

func TestToolSchema(t *testing.T) {
	runner := New(t.TempDir(), "", "", t.TempDir())
	tools := runner.Tools()
	if len(tools) != 1 || tools[0].Name != "tool_exec" {
		t.Fatalf("tools are %+v", tools)
	}
	if tools[0].Annotations["destructiveHint"] != true {
		t.Fatalf("annotations are %v", tools[0].Annotations)
	}
	properties := tools[0].InputSchema["properties"].(map[string]any)
	for _, required := range []string{"package", "hash", "fn", "arguments", "workdir"} {
		if _, ok := properties[required]; !ok {
			t.Fatalf("schema lacks %s: %v", required, properties)
		}
	}
}

var _ = protocol.Text

func TestExecRunsAGridToolPackage(t *testing.T) {
	python := ""
	for _, name := range []string{"python3", "python"} {
		if path, err := exec.LookPath(name); err == nil {
			python = path
			break
		}
	}
	if python == "" {
		t.Skip("no Python interpreter")
	}
	// The server ships the package folder, without the runtime: the node brings its own.
	module := "from grid_tool import tool\n\n\n@tool(read_only=True)\n" +
		"def word_count(text: str) -> str:\n" +
		"    '''Count words.'''\n" +
		"    return f'{len(text.split())} words'\n"
	payload := archive(t, map[string]string{
		"words/__init__.py": "",
		"words/tools.py":    module,
	})
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer tok" {
			writer.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = writer.Write(payload)
	}))
	defer server.Close()

	stateDir := t.TempDir()
	workdir := t.TempDir()
	runner := New(stateDir, server.URL, "tok", workdir)
	content, err := runner.Exec(context.Background(), map[string]any{
		"package": "words", "hash": "abc999", "fn": "word_count",
		"arguments": map[string]any{"text": "one two three"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if content[0].Text != "3 words" {
		t.Fatalf("tool answered %q", content[0].Text)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "runtime", "grid_tool.py")); err != nil {
		t.Fatalf("the embedded runtime was not written: %v", err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "tools", "abc999", "__init__.py")); err != nil {
		t.Fatalf("the package was not flattened: %v", err)
	}
}

func TestFlattenLiftsOneFolder(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "pkg"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "pkg", "a.py"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := flatten(dir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "a.py")); err != nil {
		t.Fatalf("flatten left %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "pkg")); !os.IsNotExist(err) {
		t.Fatal("flatten kept the nested folder")
	}
}

func TestUnwrapToolAnswer(t *testing.T) {
	if got, ok := unwrapToolAnswer(`{"ok":true,"result":"done"}`); !ok || got != "done" {
		t.Fatalf("unwrap gave %q (%v)", got, ok)
	}
	if got, ok := unwrapToolAnswer(`{"ok":false,"error":"boom"}`); !ok || got != "Error: boom" {
		t.Fatalf("unwrap gave %q (%v)", got, ok)
	}
	if got, ok := unwrapToolAnswer(`plain`); ok || got != "plain" {
		t.Fatalf("plain output was unwrapped: %q", got)
	}
}
