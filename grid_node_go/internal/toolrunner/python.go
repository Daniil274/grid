package toolrunner

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"gridnode/internal/state"
)

// newReader wraps downloaded bytes as a reader.
func newReader(data []byte) io.Reader { return bytes.NewReader(data) }

// Interpreter answers how to run Python on this device: the device's own
// interpreter, or a portable one (or uv) fetched from the server.
func (r *Runner) Interpreter(ctx context.Context, allowDownload bool) ([]string, error) {
	r.pythonMu.Lock()
	defer r.pythonMu.Unlock()
	if len(r.python) > 0 && executable(r.python[0]) {
		return r.python, nil
	}
	r.python = nil
	if path := state.PythonPath(r.StateDir); executable(path) {
		r.python = []string{path}
		return r.python, nil
	}
	for _, name := range []string{"python3", "python", "python3.12", "python3.11"} {
		if path, err := exec.LookPath(name); err == nil {
			r.python = []string{path}
			return r.python, nil
		}
	}
	if runtime.GOOS == "windows" {
		if path, err := exec.LookPath("py"); err == nil {
			r.python = []string{path, "-3"}
			return r.python, nil
		}
	}
	if !allowDownload {
		return nil, fmt.Errorf("no Python interpreter found on the device")
	}
	pythonErr := r.downloadPython(ctx)
	if pythonErr == nil {
		path := state.PythonPath(r.StateDir)
		if executable(path) {
			r.python = []string{path}
			return r.python, nil
		}
		pythonErr = fmt.Errorf("the portable runtime has no %s", path)
	}
	uvPath, uvErr := r.downloadUV(ctx)
	if uvErr == nil {
		r.python = []string{uvPath, "run", "--python", "3.12", "--no-project"}
		return r.python, nil
	}
	return nil, fmt.Errorf(
		"no Python interpreter on the device, and no portable runtime could be fetched (python: %v; uv: %v)",
		pythonErr, uvErr,
	)
}

// downloadPython fetches <server>/node/runtime/python-<os>-<arch>.tar.gz.
func (r *Runner) downloadPython(ctx context.Context) error {
	url := fmt.Sprintf("%s/node/runtime/python-%s-%s.tar.gz", strings.TrimRight(r.Server, "/"), runtime.GOOS, runtime.GOARCH)
	body, err := r.download(ctx, url)
	if err != nil {
		return err
	}
	runtimeDir := state.RuntimeDir(r.StateDir)
	if err := os.MkdirAll(runtimeDir, 0o755); err != nil {
		return err
	}
	temp, err := os.MkdirTemp(runtimeDir, ".python-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	if err := ExtractTarGz(newReader(body), temp); err != nil {
		return err
	}
	found := findPython(temp)
	if found == "" {
		return fmt.Errorf("the downloaded runtime holds no Python interpreter")
	}
	root := filepath.Dir(found)
	if runtime.GOOS != "windows" && filepath.Base(filepath.Dir(found)) == "bin" {
		root = filepath.Dir(filepath.Dir(found))
	}
	destination := state.PythonRuntimeDir(r.StateDir)
	_ = os.RemoveAll(destination)
	if err := os.Rename(root, destination); err != nil {
		if copyErr := copyTree(root, destination); copyErr != nil {
			return copyErr
		}
	}
	if r.Log != nil {
		r.Log.Printf("portable Python unpacked into %s", destination)
	}
	return nil
}

// downloadUV fetches <server>/node/runtime/uv-<os>-<arch>.tar.gz as a fallback
// interpreter: uv brings its own Python on first use.
func (r *Runner) downloadUV(ctx context.Context) (string, error) {
	url := fmt.Sprintf("%s/node/runtime/uv-%s-%s.tar.gz", strings.TrimRight(r.Server, "/"), runtime.GOOS, runtime.GOARCH)
	body, err := r.download(ctx, url)
	if err != nil {
		return "", err
	}
	dir := filepath.Join(state.RuntimeDir(r.StateDir), "uv")
	_ = os.RemoveAll(dir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	if err := ExtractTarGz(newReader(body), dir); err != nil {
		return "", err
	}
	name := "uv"
	if runtime.GOOS == "windows" {
		name = "uv.exe"
	}
	found := ""
	_ = filepath.WalkDir(dir, func(path string, entry os.DirEntry, err error) error {
		if err != nil || found != "" {
			return nil
		}
		if !entry.IsDir() && entry.Name() == name {
			found = path
			return filepath.SkipAll
		}
		return nil
	})
	if found == "" {
		return "", fmt.Errorf("the downloaded uv archive holds no uv binary")
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(found, 0o755)
	}
	if r.Log != nil {
		r.Log.Printf("portable uv unpacked into %s", dir)
	}
	return found, nil
}

// download fetches a runtime archive with the device's token.
func (r *Runner) download(ctx context.Context, url string) ([]byte, error) {
	if r.Server == "" || r.Token == "" {
		return nil, fmt.Errorf("no --server and token to download it from")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+r.Token)
	request.Header.Set("User-Agent", "grid-node/"+Version)
	response, err := r.Client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		head, _ := io.ReadAll(io.LimitReader(response.Body, 256))
		return nil, fmt.Errorf("HTTP %d from %s: %s", response.StatusCode, url, strings.TrimSpace(string(head)))
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 512*1024*1024))
	if err != nil {
		return nil, err
	}
	return data, nil
}

// findPython looks for bin/python3 or python.exe under root.
func findPython(root string) string {
	candidate := ""
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil || candidate != "" {
			return nil
		}
		if entry.IsDir() {
			if entry.Name() == "__pycache__" {
				return filepath.SkipDir
			}
			return nil
		}
		name := entry.Name()
		if name == "python3" || name == "python3.12" || name == "python.exe" {
			if executable(path) {
				candidate = path
				return filepath.SkipAll
			}
		}
		return nil
	})
	return candidate
}

func executable(path string) bool {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return false
	}
	if runtime.GOOS == "windows" {
		return true
	}
	return info.Mode()&0o111 != 0
}

func copyTree(source, destination string) error {
	_ = os.RemoveAll(destination)
	return filepath.WalkDir(source, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		target := filepath.Join(destination, relative)
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if info.Mode()&os.ModeSymlink != 0 {
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		}
		payload, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, payload, info.Mode().Perm())
	})
}
