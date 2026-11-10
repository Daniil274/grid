package shelldriver

import (
	"context"
	"encoding/json"
	"runtime"
	"strings"
	"testing"
	"time"

	"gridnode/internal/fsdriver"
	"gridnode/internal/protocol"
)

func newDriver(t *testing.T) (*Driver, string) {
	t.Helper()
	workdir := t.TempDir()
	return New(fsdriver.New(workdir)), workdir
}

func decodeResult(t *testing.T, content []protocol.Content) Result {
	t.Helper()
	var result Result
	if err := json.Unmarshal([]byte(content[0].Text), &result); err != nil {
		t.Fatalf("answer is not JSON: %v (%s)", err, content[0].Text)
	}
	return result
}

func TestExecRunsInTheWorkingDir(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh is not available on Windows")
	}
	driver, workdir := newDriver(t)
	content, err := driver.Exec(context.Background(), map[string]any{
		"command": "printf 'cwd=%s\\n' \"$(pwd)\"", "working_dir": ".",
	})
	if err != nil {
		t.Fatal(err)
	}
	result := decodeResult(t, content)
	if result.ExitCode != 0 {
		t.Fatalf("exit code %d: %s", result.ExitCode, result.Stderr)
	}
	if !strings.Contains(result.Stdout, workdir) {
		t.Fatalf("stdout %q does not name %s", result.Stdout, workdir)
	}
}

func TestExecReportsExitCodeAndStderr(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("sh is not available on Windows")
	}
	driver, _ := newDriver(t)
	content, err := driver.Exec(context.Background(), map[string]any{
		"command": "echo problem >&2; exit 3",
	})
	if err != nil {
		t.Fatal(err)
	}
	result := decodeResult(t, content)
	if result.ExitCode != 3 {
		t.Fatalf("exit code is %d", result.ExitCode)
	}
	if !strings.Contains(result.Stderr, "problem") {
		t.Fatalf("stderr is %q", result.Stderr)
	}
}

func TestExecKillsTheTreeOnTimeout(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses sh sleep")
	}
	driver, _ := newDriver(t)
	started := time.Now()
	content, err := driver.Exec(context.Background(), map[string]any{
		"command": "sleep 20", "timeout": 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	elapsed := time.Since(started)
	if elapsed > 10*time.Second {
		t.Fatalf("timeout took %s", elapsed)
	}
	result := decodeResult(t, content)
	if !result.TimedOut || result.ExitCode != -1 {
		t.Fatalf("result is %+v", result)
	}
	if !strings.Contains(result.Stderr, "timed out") {
		t.Fatalf("stderr is %q", result.Stderr)
	}
}

func TestExecRefusesOutsideTheWorkdir(t *testing.T) {
	driver, _ := newDriver(t)
	if _, err := driver.Exec(context.Background(), map[string]any{"command": "true", "working_dir": "../"}); err == nil {
		t.Fatal("a working dir above the workdir was allowed")
	}
}

func TestToolSchemaIsRun(t *testing.T) {
	driver, _ := newDriver(t)
	tools := driver.Tools()
	if len(tools) != 1 || tools[0].Name != "shell_exec" {
		t.Fatalf("tools are %+v", tools)
	}
	if tools[0].Annotations["destructiveHint"] != true {
		t.Fatalf("annotations are %v", tools[0].Annotations)
	}
}
