package toolrunner

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"gridnode/internal/protocol"
	"gridnode/internal/state"
)

// Runner serves tool_exec for one node.
type Runner struct {
	StateDir string
	Server   string
	Token    string
	Workdir  string
	Client   *http.Client
	Log      *log.Logger

	pythonMu sync.Mutex
	python   []string
}

// New makes the runner; server and token may be empty, then only cached
// packages can run.
func New(stateDir, server, token, workdir string) *Runner {
	return &Runner{
		StateDir: stateDir,
		Server:   server,
		Token:    token,
		Workdir:  workdir,
		Client:   &http.Client{Timeout: 300 * time.Second},
	}
}

// Tools is what this driver brings to a node.
func (r *Runner) Tools() []*protocol.Tool {
	return []*protocol.Tool{{
		Name: "tool_exec",
		Description: "Run a function of a tool package on the device. The package is fetched from the Grid " +
			"server by its hash the first time and then cached; a portable Python is fetched when the device " +
			"has none.",
		InputSchema: protocol.Object(map[string]any{
			"package":   protocol.PString("The package name, e.g. words."),
			"hash":      protocol.PString("The package's content hash (which version to run)."),
			"fn":        protocol.PString("The function (tool) of the package to call."),
			"arguments": map[string]any{"type": "object", "description": "The function's arguments.", "default": map[string]any{}},
			"workdir":   protocol.PString("Where the function runs; defaults to the node's workspace."),
		}, "package", "hash", "fn"),
		Annotations: protocol.Run,
		Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
			return r.Exec(ctx, args)
		},
	}}
}

// Exec implements tool_exec.
func (r *Runner) Exec(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
	packageName, err := protocol.RequiredString(args, "package")
	if err != nil {
		return nil, err
	}
	hash, err := protocol.RequiredString(args, "hash")
	if err != nil {
		return nil, err
	}
	if !safeHash.MatchString(hash) || strings.ContainsAny(hash, `/\`) {
		return nil, fmt.Errorf("hash %q is not a plain identifier", hash)
	}
	fn, err := protocol.RequiredString(args, "fn")
	if err != nil {
		return nil, err
	}
	functionArgs := protocol.ObjectArg(args, "arguments")
	if raw, ok := args["arguments"].(string); ok && strings.TrimSpace(raw) != "" {
		parsed := map[string]any{}
		if err := json.Unmarshal([]byte(raw), &parsed); err == nil {
			functionArgs = parsed
		}
	}
	dir := state.ToolDir(r.StateDir, hash)
	if _, err := os.Stat(filepath.Join(dir, ".grid-ready")); err != nil {
		if err := r.EnsurePackage(ctx, packageName, hash, dir); err != nil {
			return nil, err
		}
	}
	workdir := strings.TrimSpace(protocol.String(args, "workdir", ""))
	if workdir == "" {
		workdir = r.Workdir
	}
	workdir = state.ExpandHome(workdir)
	if info, err := os.Stat(workdir); err != nil || !info.IsDir() {
		workdir = dir
	}
	output, err := r.runTool(ctx, dir, packageName, fn, functionArgs, workdir)
	if err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(output)}, nil
}

// EnsurePackage downloads and unpacks the package when it is not cached.
func (r *Runner) EnsurePackage(ctx context.Context, packageName, hash, dir string) error {
	if err := os.MkdirAll(filepath.Dir(dir), 0o755); err != nil {
		return err
	}
	if _, err := os.Stat(dir); err == nil {
		// An unpacked but incomplete package: start over.
		_ = os.RemoveAll(dir)
	}
	if r.Server == "" || r.Token == "" {
		return fmt.Errorf("tool package %s (%s) is not cached and no server is configured to download it", packageName, hash)
	}
	url := toolURL(r.Server, packageName, hash)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+r.Token)
	request.Header.Set("User-Agent", "grid-node/"+Version)
	response, err := r.Client.Do(request)
	if err != nil {
		return fmt.Errorf("cannot download %s: %w", url, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 512))
		return fmt.Errorf("cannot download %s: HTTP %d %s", url, response.StatusCode, strings.TrimSpace(string(body)))
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 512*1024*1024))
	if err != nil {
		return fmt.Errorf("cannot download %s: %w", url, err)
	}
	// When the URL names the whole sha256, the download must be exactly it.
	if len(hash) == 64 && isHex(hash) {
		actual := sha256.Sum256(data)
		if !strings.EqualFold(hex.EncodeToString(actual[:]), hash) {
			return fmt.Errorf("the downloaded %s does not match its hash %s", packageName, hash)
		}
	}
	temp, err := os.MkdirTemp(filepath.Dir(dir), ".download-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	if err := ExtractTarGz(bytes.NewReader(data), temp); err != nil {
		return fmt.Errorf("cannot unpack %s: %w", packageName, err)
	}
	if err := flatten(temp); err != nil {
		return err
	}
	if err := os.RemoveAll(dir); err != nil {
		return err
	}
	if err := os.Rename(temp, dir); err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, ".grid-ready"), []byte(hash), 0o644); err != nil {
		return err
	}
	if r.Log != nil {
		r.Log.Printf("tool package %s (%s) unpacked into %s", packageName, hash, dir)
	}
	return nil
}

// runTool chooses how to run the function: tool.json's command, its entry, or
// a Grid Python package's grid_tool.py.
func (r *Runner) runTool(ctx context.Context, dir, packageName, fn string, args map[string]any, workdir string) (string, error) {
	payload, err := json.Marshal(args)
	if err != nil {
		return "", err
	}
	var manifestData manifest
	if raw, err := os.ReadFile(filepath.Join(dir, "tool.json")); err == nil {
		if err := json.Unmarshal(raw, &manifestData); err != nil {
			return "", fmt.Errorf("tool.json is not JSON: %w", err)
		}
	}
	command, err := r.commandFor(dir, packageName, fn, manifestData)
	if err != nil {
		return "", err
	}
	timeout := manifestData.Timeout
	if timeout <= 0 {
		timeout = 300
	}
	runContext, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(runContext, command[0], command[1:]...)
	cmd.Dir = workdir
	cmd.Stdin = bytes.NewReader(payload)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	prepareProcess(cmd)
	err = cmd.Run()
	output := strings.TrimSpace(stdout.String())
	if err != nil {
		if runContext.Err() != nil {
			return "", fmt.Errorf("tool %s.%s timed out after %d seconds", packageName, fn, timeout)
		}
		detail := strings.TrimSpace(stderr.String())
		if detail == "" {
			detail = err.Error()
		}
		if output != "" {
			detail = output + "\n" + detail
		}
		return "", fmt.Errorf("tool %s.%s failed: %s", packageName, fn, truncate(detail, 4000))
	}
	if unwrapped, ok := unwrapToolAnswer(output); ok {
		output = unwrapped
	}
	if output == "" {
		output = "(no output)"
	}
	if warning := strings.TrimSpace(stderr.String()); warning != "" && r.Log != nil {
		r.Log.Printf("tool %s.%s stderr: %s", packageName, fn, truncate(warning, 400))
	}
	return output, nil
}

func (r *Runner) commandFor(dir, packageName, fn string, spec manifest) ([]string, error) {
	substitute := func(value string) string {
		replaced := strings.ReplaceAll(value, "${fn}", fn)
		replaced = strings.ReplaceAll(replaced, "${dir}", dir)
		replaced = strings.ReplaceAll(replaced, "${package}", packageName)
		return replaced
	}
	if line := spec.commandLine(); len(line) > 0 {
		for index := range line {
			line[index] = substitute(line[index])
			candidate := line[index]
			if index == 0 && !filepath.IsAbs(candidate) && strings.Contains(candidate, string(filepath.Separator)) {
				line[index] = filepath.Join(dir, candidate)
			}
		}
		if spec.Function {
			line = append(line, fn)
		}
		line = append(line, spec.Args...)
		return line, nil
	}
	if spec.Entry != "" {
		entry := substitute(spec.Entry)
		if !filepath.IsAbs(entry) {
			entry = filepath.Join(dir, entry)
		}
		if _, err := os.Stat(entry); err != nil {
			return nil, fmt.Errorf("tool entry %s does not exist", entry)
		}
		if spec.Runtime == "python" || strings.HasSuffix(strings.ToLower(entry), ".py") {
			python, err := r.Interpreter(ctxOrBackground(), true)
			if err != nil {
				return nil, err
			}
			return append(append(append([]string{}, python...), entry, fn), spec.Args...), nil
		}
		return append([]string{entry, fn}, spec.Args...), nil
	}
	// A Grid tool package: the modules plus the node's own grid_tool runtime.
	script := filepath.Join(dir, "grid_tool.py")
	if spec.Script != "" {
		script = filepath.Join(dir, spec.Script)
	}
	if _, err := os.Stat(script); err == nil || detectPackage(dir) != "" {
		python, err := r.Interpreter(ctxOrBackground(), true)
		if err != nil {
			return nil, err
		}
		if _, err := os.Stat(script); err != nil {
			script, err = r.GridToolScript()
			if err != nil {
				return nil, err
			}
		}
		packagePath := spec.Package
		if packagePath == "" {
			packagePath = detectPackage(dir)
		}
		if packagePath == "" {
			return nil, fmt.Errorf("package %s has no Python package inside", packageName)
		}
		if !filepath.IsAbs(packagePath) {
			packagePath = filepath.Join(dir, packagePath)
		}
		return append(append([]string{}, python...), script, "call", packagePath, fn), nil
	}
	for _, name := range []string{"run.sh", "run", "run.exe", "tool"} {
		candidate := filepath.Join(dir, name)
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			if runtime.GOOS != "windows" {
				_ = os.Chmod(candidate, 0o755)
			}
			return []string{candidate, fn}, nil
		}
	}
	return nil, fmt.Errorf("package %s has no tool.json, no grid_tool.py and no run script", packageName)
}

// detectPackage finds the folder of Python modules to hand grid_tool.py.
func detectPackage(dir string) string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return ""
	}
	for _, entry := range entries {
		if !entry.IsDir() || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		if _, err := os.Stat(filepath.Join(dir, entry.Name(), "__init__.py")); err == nil {
			return entry.Name()
		}
	}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".py") || entry.Name() == "grid_tool.py" {
			continue
		}
		return "."
	}
	return ""
}

// unwrapToolAnswer reads what grid_tool.py answers: {"ok": true, "result": ...}
// or {"ok": false, "error": ...}.
func unwrapToolAnswer(output string) (string, bool) {
	if !strings.HasPrefix(strings.TrimSpace(output), "{") {
		return output, false
	}
	var answer struct {
		OK     *bool           `json:"ok"`
		Result json.RawMessage `json:"result"`
		Error  string          `json:"error"`
	}
	if err := json.Unmarshal([]byte(output), &answer); err != nil || answer.OK == nil {
		return output, false
	}
	if !*answer.OK {
		if answer.Error == "" {
			answer.Error = "the tool failed"
		}
		return "Error: " + answer.Error, true
	}
	if len(answer.Result) == 0 {
		return "", true
	}
	var text string
	if err := json.Unmarshal(answer.Result, &text); err == nil {
		return text, true
	}
	return string(answer.Result), true
}

// flatten lifts the single top-level folder of an archive (the package's own
// name) up, so the package's files sit at the root of its cache directory.
func flatten(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	files := []os.DirEntry{}
	folders := []os.DirEntry{}
	for _, entry := range entries {
		if entry.IsDir() {
			folders = append(folders, entry)
		} else {
			files = append(files, entry)
		}
	}
	if len(folders) != 1 || len(files) != 0 {
		return nil
	}
	nested := filepath.Join(dir, folders[0].Name())
	inner, err := os.ReadDir(nested)
	if err != nil {
		return err
	}
	for _, entry := range inner {
		if err := os.Rename(filepath.Join(nested, entry.Name()), filepath.Join(dir, entry.Name())); err != nil {
			return err
		}
	}
	return os.Remove(nested)
}

// isHex reports whether value is plain lowercase or uppercase hex.
func isHex(value string) bool {
	for _, character := range value {
		switch {
		case character >= '0' && character <= '9':
		case character >= 'a' && character <= 'f':
		case character >= 'A' && character <= 'F':
		default:
			return false
		}
	}
	return true
}

func truncate(text string, size int) string {
	if len(text) <= size {
		return text
	}
	return text[:size] + "…"
}

// Version is the node's protocol version, also sent as a user agent.
const Version = "0.3.0-go"

// ctxOrBackground stands in where a plain context is needed.
func ctxOrBackground() context.Context { return context.Background() }
