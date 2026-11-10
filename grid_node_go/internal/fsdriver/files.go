package fsdriver

import (
	"bufio"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"gridnode/internal/protocol"
)

const (
	// MaxTextBytes caps what one fs_read hands back as text.
	MaxTextBytes = 8 * 1024 * 1024
	// MaxTextChars caps the characters of one text part.
	MaxTextChars = 400_000
)

var imageMimes = map[string]string{
	".png":  "image/png",
	".jpg":  "image/jpeg",
	".jpeg": "image/jpeg",
	".gif":  "image/gif",
	".webp": "image/webp",
	".bmp":  "image/bmp",
	".ico":  "image/x-icon",
}

// Read implements fs_read: text with offset/limit_lines, images as MCP images.
func (d *Driver) Read(args map[string]any) ([]protocol.Content, error) {
	path, err := protocol.RequiredString(args, "filepath")
	if err != nil {
		return nil, err
	}
	target, err := d.Resolve(args, path)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(target)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	if info.IsDir() {
		return nil, fmt.Errorf("%s is a directory; use fs_list_dirs or fs_glob", path)
	}
	if info.Size() > 256*1024*1024 {
		return nil, fmt.Errorf("%s is %d bytes; that is too large to read", path, info.Size())
	}
	mime := imageMimes[strings.ToLower(filepath.Ext(target))]
	if mime != "" {
		data, err := os.ReadFile(target)
		if err != nil {
			return nil, err
		}
		return []protocol.Content{protocol.Text(fmt.Sprintf("%s (%s, %d bytes)", path, mime, len(data))), protocol.Image(data, mime)}, nil
	}
	file, err := os.Open(target)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	offset := protocol.Int(args, "offset", 0)
	if offset < 0 {
		offset = 0
	}
	limit := protocol.Int(args, "limit_lines", 0)
	if limit < 0 {
		limit = 0
	}

	head := make([]byte, 8192)
	read, _ := file.Read(head)
	if _, err := file.Seek(0, 0); err != nil {
		return nil, err
	}
	if read > 0 && (bytesHaveNUL(head[:read]) || !utf8.Valid(head[:read])) {
		data, err := os.ReadFile(target)
		if err != nil {
			return nil, err
		}
		if len(data) > MaxTextChars {
			data = data[:MaxTextChars]
		}
		return []protocol.Content{protocol.Text(fmt.Sprintf(
			"Binary file %s (%d bytes); base64 of the first %d bytes:\n%s",
			path, info.Size(), len(data), base64.StdEncoding.EncodeToString(data)))}, nil
	}

	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	var lines []string
	lineNumber := 0
	truncated := false
	totalChars := 0
	for scanner.Scan() {
		lineNumber++
		if lineNumber <= offset {
			continue
		}
		if limit > 0 && len(lines) >= limit {
			break
		}
		text := scanner.Text()
		totalChars += len(text) + 1
		if totalChars > MaxTextChars {
			truncated = true
			break
		}
		lines = append(lines, text)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	if len(lines) == 0 {
		if offset > 0 {
			return []protocol.Content{protocol.Text(fmt.Sprintf("%s: no lines at offset %d", path, offset))}, nil
		}
		return []protocol.Content{protocol.Text("")}, nil
	}
	body := strings.Join(lines, "\n")
	if truncated {
		body += fmt.Sprintf("\n… [truncated at %d characters; read on with offset=%d]", MaxTextChars, offset+len(lines))
	}
	return []protocol.Content{protocol.Text(body)}, nil
}

func bytesHaveNUL(data []byte) bool {
	for _, value := range data {
		if value == 0 {
			return true
		}
	}
	return false
}

// Write implements fs_write: create parents, refuse to replace without overwrite.
func (d *Driver) Write(args map[string]any) ([]protocol.Content, error) {
	path, err := protocol.RequiredString(args, "filepath")
	if err != nil {
		return nil, err
	}
	content, ok := args["content"].(string)
	if !ok {
		if args["content"] == nil {
			content = ""
		} else {
			return nil, errors.New("content must be a string")
		}
	}
	target, err := d.Resolve(args, path)
	if err != nil {
		return nil, err
	}
	overwrite := protocol.Bool(args, "overwrite", false)
	if info, err := os.Stat(target); err == nil && info.IsDir() {
		return nil, fmt.Errorf("%s is a directory", path)
	} else if err == nil && !overwrite {
		return nil, fmt.Errorf("file '%s' exists; pass overwrite=true to replace it", path)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return nil, err
	}
	if err := os.WriteFile(target, []byte(content), 0o644); err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(fmt.Sprintf("Wrote %d bytes to %s.", len([]byte(content)), path))}, nil
}

// Append implements fs_append, starting a new line when the file needs one.
func (d *Driver) Append(args map[string]any) ([]protocol.Content, error) {
	path, err := protocol.RequiredString(args, "filepath")
	if err != nil {
		return nil, err
	}
	content, ok := args["content"].(string)
	if !ok {
		if args["content"] == nil {
			content = ""
		} else {
			return nil, errors.New("content must be a string")
		}
	}
	target, err := d.Resolve(args, path)
	if err != nil {
		return nil, err
	}
	if info, err := os.Stat(target); err == nil && info.IsDir() {
		return nil, fmt.Errorf("%s is a directory", path)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return nil, err
	}
	prefix := ""
	if info, err := os.Stat(target); err == nil && info.Size() > 0 {
		tail, err := readTail(target, 1)
		if err != nil {
			return nil, err
		}
		if len(tail) > 0 && !strings.HasSuffix(tail, "\n") {
			prefix = "\n"
		}
	}
	file, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	payload := prefix + content
	if _, err := file.WriteString(payload); err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(fmt.Sprintf("Appended %d bytes to %s.", len(payload), path))}, nil
}

func readTail(path string, size int64) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	from := info.Size() - size
	if from < 0 {
		from = 0
	}
	if _, err := file.Seek(from, 0); err != nil {
		return "", err
	}
	data, err := io.ReadAll(io.LimitReader(file, info.Size()-from))
	if err != nil {
		return "", err
	}
	return string(data), nil
}

// Edit implements fs_edit: apply a unified diff to the file.
func (d *Driver) Edit(args map[string]any) ([]protocol.Content, error) {
	path, err := protocol.RequiredString(args, "filepath")
	if err != nil {
		return nil, err
	}
	patch, err := protocol.RequiredString(args, "patch_content")
	if err != nil {
		return nil, err
	}
	target, err := d.Resolve(args, path)
	if err != nil {
		return nil, err
	}
	original, err := os.ReadFile(target)
	if err != nil {
		return nil, fmt.Errorf("cannot read %s: %w", path, err)
	}
	updated, hunks, err := ApplyUnifiedDiff(string(original), patch)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(target, []byte(updated), 0o644); err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(fmt.Sprintf("Applied %d hunk(s) to %s.", hunks, path))}, nil
}
