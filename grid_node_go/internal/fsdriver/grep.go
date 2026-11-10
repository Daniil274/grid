package fsdriver

import (
	"bufio"
	"bytes"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"

	"gridnode/internal/protocol"
)

const (
	grepMaxFileBytes = 8 * 1024 * 1024
	grepWorkers      = 8
)

type grepHit struct {
	Path string
	Line int
	Text string
}

// Grep implements fs_grep: a concurrent regexp/literal search under directory.
func (d *Driver) Grep(args map[string]any) ([]protocol.Content, error) {
	pattern, err := protocol.RequiredString(args, "pattern")
	if err != nil {
		return nil, err
	}
	directory := protocol.String(args, "directory", ".")
	root, err := d.Resolve(args, directory)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(root)
	if err != nil {
		return nil, fmt.Errorf("cannot search %s: %w", directory, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%s is not a directory", directory)
	}
	caseSensitive := protocol.Bool(args, "case_sensitive", false)
	useRegex := protocol.Bool(args, "use_regex", true)
	maxResults := protocol.Clamp(protocol.Int(args, "max_results", 100), 1, 10000)

	extensions := map[string]bool{}
	for _, raw := range strings.Split(protocol.String(args, "file_extensions", ""), ",") {
		value := strings.ToLower(strings.TrimSpace(raw))
		if value == "" {
			continue
		}
		if !strings.HasPrefix(value, ".") {
			value = "." + value
		}
		extensions[value] = true
	}

	var matcher func(string) bool
	if useRegex {
		expression := pattern
		if !caseSensitive {
			expression = "(?i)" + expression
		}
		compiled, err := regexp.Compile(expression)
		if err != nil {
			return nil, fmt.Errorf("bad regexp %q: %w", pattern, err)
		}
		matcher = compiled.MatchString
	} else if caseSensitive {
		matcher = func(line string) bool { return strings.Contains(line, pattern) }
	} else {
		lowered := strings.ToLower(pattern)
		matcher = func(line string) bool { return strings.Contains(strings.ToLower(line), lowered) }
	}

	files := make(chan string, 256)
	var hitsMu sync.Mutex
	hits := []grepHit{}
	var count int64

	workers := grepWorkers
	if value := runtime.NumCPU(); value < workers {
		workers = value
	}
	if workers < 1 {
		workers = 1
	}
	var group sync.WaitGroup
	for index := 0; index < workers; index++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for path := range files {
				if atomic.LoadInt64(&count) >= int64(maxResults) {
					continue
				}
				fileHits := grepFile(path, root, matcher, maxResults, &count)
				if len(fileHits) == 0 {
					continue
				}
				hitsMu.Lock()
				hits = append(hits, fileHits...)
				hitsMu.Unlock()
			}
		}()
	}

	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErrLocal error) error {
		if walkErrLocal != nil {
			return nil
		}
		if entry.IsDir() {
			if path != root && skipDir(entry.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if atomic.LoadInt64(&count) >= int64(maxResults) {
			return fs.SkipAll
		}
		if len(extensions) > 0 && !extensions[strings.ToLower(filepath.Ext(path))] {
			return nil
		}
		info, err := entry.Info()
		if err != nil || info.Size() > grepMaxFileBytes || info.Size() == 0 {
			return nil
		}
		select {
		case files <- path:
		default:
			// Keep the walk moving even if workers are behind.
			files <- path
		}
		return nil
	})
	close(files)
	group.Wait()
	if err != nil && err != fs.SkipAll {
		return nil, err
	}

	sort.Slice(hits, func(first, second int) bool {
		if hits[first].Path != hits[second].Path {
			return hits[first].Path < hits[second].Path
		}
		return hits[first].Line < hits[second].Line
	})
	if len(hits) == 0 {
		return []protocol.Content{protocol.Text(fmt.Sprintf("No matches for %q in %s.", pattern, directory))}, nil
	}
	lines := make([]string, 0, len(hits))
	for _, hit := range hits {
		lines = append(lines, fmt.Sprintf("%s:%d: %s", hit.Path, hit.Line, hit.Text))
	}
	header := fmt.Sprintf("%d match(es) for %q in %s:", len(hits), pattern, directory)
	if len(hits) >= maxResults {
		header = fmt.Sprintf("%d match(es) (limit reached) for %q in %s:", len(hits), pattern, directory)
	}
	return []protocol.Content{protocol.Text(header + "\n" + strings.Join(lines, "\n"))}, nil
}

func grepFile(path, root string, matcher func(string) bool, maxResults int, count *int64) []grepHit {
	file, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer file.Close()
	head := make([]byte, 8192)
	read, _ := file.Read(head)
	if read > 0 && bytes.IndexByte(head[:read], 0) >= 0 {
		return nil
	}
	if _, err := file.Seek(0, 0); err != nil {
		return nil
	}
	relative := filepath.ToSlash(Relative(root, path))
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	hits := []grepHit{}
	lineNumber := 0
	for scanner.Scan() {
		lineNumber++
		text := scanner.Text()
		if !matcher(text) {
			continue
		}
		if atomic.AddInt64(count, 1) > int64(maxResults) {
			break
		}
		if len(text) > 500 {
			text = text[:500] + "…"
		}
		hits = append(hits, grepHit{Path: relative, Line: lineNumber, Text: text})
		if len(hits) >= maxResults {
			break
		}
	}
	return hits
}
