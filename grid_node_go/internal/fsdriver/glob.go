package fsdriver

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"gridnode/internal/protocol"
)

// skippedDirs are not walked: they are huge and never what an agent wants.
var skippedDirs = map[string]bool{
	".git": true, ".hg": true, ".svn": true,
	"node_modules": true, "__pycache__": true, ".venv": true, "venv": true,
	".mypy_cache": true, ".ruff_cache": true, ".pytest_cache": true,
	".idea": true, ".tox": true, ".gradle": true, ".cache": true,
}

func skipDir(name string) bool {
	return skippedDirs[strings.ToLower(name)]
}

// globRegexp turns a glob with ** and / into a regexp over slash paths.
func globRegexp(pattern string) (*regexp.Regexp, bool, error) {
	pattern = filepath.ToSlash(pattern)
	baseOnly := !strings.Contains(pattern, "/")
	var out strings.Builder
	if baseOnly {
		out.WriteString(`(?:^|/)`)
	} else {
		out.WriteString(`^`)
	}
	for index := 0; index < len(pattern); index++ {
		char := pattern[index]
		switch char {
		case '*':
			if index+1 < len(pattern) && pattern[index+1] == '*' {
				index++
				if index+1 < len(pattern) && pattern[index+1] == '/' {
					index++
					out.WriteString(`(?:.*/)?`)
				} else {
					out.WriteString(`.*`)
				}
				continue
			}
			out.WriteString(`[^/]*`)
		case '?':
			out.WriteString(`[^/]`)
		case '[':
			end := index + 1
			if end < len(pattern) && (pattern[end] == '!' || pattern[end] == '^') {
				end++
			}
			if end < len(pattern) && pattern[end] == ']' {
				end++
			}
			for end < len(pattern) && pattern[end] != ']' {
				end++
			}
			if end >= len(pattern) {
				out.WriteString(`\[`)
				continue
			}
			class := pattern[index+1 : end]
			if strings.HasPrefix(class, "!") {
				class = "^" + class[1:]
			}
			out.WriteString("[" + class + "]")
			index = end
		case '\\':
			if index+1 < len(pattern) {
				index++
				out.WriteString(regexp.QuoteMeta(string(pattern[index])))
			}
		default:
			out.WriteString(regexp.QuoteMeta(string(char)))
		}
	}
	out.WriteString(`$`)
	compiled, err := regexp.Compile(out.String())
	return compiled, baseOnly, err
}

// Glob implements fs_glob: files and directories under directory matching pattern.
func (d *Driver) Glob(args map[string]any) ([]protocol.Content, error) {
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
	matcher, _, err := globRegexp(pattern)
	if err != nil {
		return nil, fmt.Errorf("bad pattern %q: %w", pattern, err)
	}
	maxResults := protocol.Clamp(protocol.Int(args, "max_results", 100), 1, 5000)
	matches := []string{}
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if entry.IsDir() {
			if path != root && skipDir(entry.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		relative := filepath.ToSlash(Relative(root, path))
		if matcher.MatchString(relative) {
			matches = append(matches, relative)
			if len(matches) >= maxResults {
				return fs.SkipAll
			}
		}
		return nil
	})
	if err != nil && err != fs.SkipAll {
		return nil, err
	}
	sort.Strings(matches)
	if len(matches) == 0 {
		return []protocol.Content{protocol.Text(fmt.Sprintf("No files matched %q in %s.", pattern, directory))}, nil
	}
	header := fmt.Sprintf("%d match(es) for %q in %s:", len(matches), pattern, directory)
	if len(matches) >= maxResults {
		header = fmt.Sprintf("%d match(es) (limit reached) for %q in %s:", len(matches), pattern, directory)
	}
	return []protocol.Content{protocol.Text(header + "\n" + strings.Join(matches, "\n"))}, nil
}
