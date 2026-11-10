// Package selfupdate replaces the running binary with a newer one the server
// offers on the ctl channel, then starts the new binary.
package selfupdate

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Update downloads url over the existing binary and restarts the node.
func Update(ctx context.Context, url, expectedSHA string) error {
	if strings.TrimSpace(url) == "" {
		return fmt.Errorf("update message carries no url")
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	if resolved, err := filepath.EvalSymlinks(executable); err == nil {
		executable = resolved
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	request.Header.Set("User-Agent", "grid-node/0.3.0-go")
	client := &http.Client{Timeout: 10 * time.Minute}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("cannot download the update: HTTP %d", response.StatusCode)
	}
	temporary := executable + ".new"
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return fmt.Errorf("cannot write %s: %w", temporary, err)
	}
	digest := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(file, digest), response.Body)
	closeErr := file.Close()
	if copyErr != nil {
		_ = os.Remove(temporary)
		return copyErr
	}
	if closeErr != nil {
		_ = os.Remove(temporary)
		return closeErr
	}
	if written < 1024 {
		_ = os.Remove(temporary)
		return fmt.Errorf("the downloaded binary is only %d bytes", written)
	}
	if expectedSHA != "" {
		actual := hex.EncodeToString(digest.Sum(nil))
		if !strings.EqualFold(actual, expectedSHA) {
			_ = os.Remove(temporary)
			return fmt.Errorf("the download's sha256 is %s, not %s", actual, expectedSHA)
		}
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(temporary, 0o755)
	}
	if err := replace(executable, temporary); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return restart(executable)
}

// replace swaps the new binary in for the running one.
func replace(executable, temporary string) error {
	if runtime.GOOS == "windows" {
		old := executable + ".old"
		_ = os.Remove(old)
		if err := os.Rename(executable, old); err != nil {
			return fmt.Errorf("cannot move the running binary aside: %w", err)
		}
		if err := os.Rename(temporary, executable); err != nil {
			_ = os.Rename(old, executable)
			return err
		}
		return nil
	}
	return os.Rename(temporary, executable)
}

// restart starts the new binary with the same arguments and leaves this process.
func restart(executable string) error {
	command := exec.Command(executable, os.Args[1:]...)
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		return fmt.Errorf("updated, but the new binary could not start: %w", err)
	}
	os.Exit(0)
	return nil
}
