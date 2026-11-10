// Package toolrunner runs tool packages an agent asks for: it downloads the
// package by its hash once, unpacks it under the node's state directory, and
// runs the requested function with JSON arguments on stdin - with a portable
// Python fetched from the server when the device has none.
package toolrunner

import (
	"archive/tar"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var safeHash = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// ExtractTarGz unpacks a tar.gz archive into dest, refusing entries that would
// land outside it.
func ExtractTarGz(reader io.Reader, dest string) error {
	gzipReader, err := gzip.NewReader(reader)
	if err != nil {
		return fmt.Errorf("not a gzip archive: %w", err)
	}
	defer gzipReader.Close()
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return err
	}
	archive := tar.NewReader(gzipReader)
	for {
		header, err := archive.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		name := filepath.Clean(filepath.FromSlash(header.Name))
		if name == "." {
			continue
		}
		if filepath.IsAbs(name) || strings.HasPrefix(name, ".."+string(filepath.Separator)) || name == ".." {
			return fmt.Errorf("archive entry %q escapes the package", header.Name)
		}
		target := filepath.Join(dest, name)
		inside, err := filepath.Rel(dest, target)
		if err != nil || inside == ".." || strings.HasPrefix(inside, ".."+string(filepath.Separator)) {
			return fmt.Errorf("archive entry %q escapes the package", header.Name)
		}
		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeSymlink:
			link := header.Linkname
			if filepath.IsAbs(link) {
				continue
			}
			resolved := filepath.Clean(filepath.Join(filepath.Dir(target), link))
			if relative, err := filepath.Rel(dest, resolved); err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
				continue
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			_ = os.Remove(target)
			if err := os.Symlink(link, target); err != nil {
				return err
			}
		case tar.TypeLink:
			source := filepath.Join(dest, filepath.Clean(filepath.FromSlash(header.Linkname)))
			if relative, err := filepath.Rel(dest, source); err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
				continue
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Link(source, target); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			mode := os.FileMode(header.Mode) & os.ModePerm
			if mode == 0 {
				mode = 0o644
			}
			file, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
			if err != nil {
				return err
			}
			if _, err := io.Copy(file, archive); err != nil {
				file.Close()
				return err
			}
			if err := file.Close(); err != nil {
				return err
			}
			_ = os.Chmod(target, mode)
		}
	}
}

// toolURL is where a package's archive lives on the server.
func toolURL(server, packageName, hash string) string {
	return strings.TrimRight(server, "/") + "/node/tools/" + url.PathEscape(packageName) + "/" + url.PathEscape(hash) + ".tar.gz"
}

// manifest is the optional tool.json of a package.
type manifest struct {
	Name     string          `json:"name"`
	Runtime  string          `json:"runtime"`
	Entry    string          `json:"entry"`
	Script   string          `json:"script"`
	Package  string          `json:"package"`
	Command  json.RawMessage `json:"command"`
	Args     []string        `json:"args"`
	Function bool            `json:"function_arg"`
	Timeout  int             `json:"timeout"`
}

// commandLine reads "command", which may be a list or one string.
func (m *manifest) commandLine() []string {
	if len(m.Command) == 0 {
		return nil
	}
	var list []string
	if err := json.Unmarshal(m.Command, &list); err == nil {
		return list
	}
	var single string
	if err := json.Unmarshal(m.Command, &single); err == nil && strings.TrimSpace(single) != "" {
		return strings.Fields(single)
	}
	return nil
}
