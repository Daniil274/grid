package fsdriver

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"gridnode/internal/protocol"
)

// Listing is what fs_list_dirs answers: enough for a folder picker in chat.
type Listing struct {
	Current        string        `json:"current"`
	Parent         string        `json:"parent"`
	DefaultWorkdir string        `json:"default_workdir"`
	Platform       string        `json:"platform"`
	Roots          []string      `json:"roots"`
	Bookmarks      []Bookmark    `json:"bookmarks"`
	Dirs           []ListingItem `json:"dirs"`
}

// Bookmark is a named place a person jumps to.
type Bookmark struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// ListingItem is one subdirectory.
type ListingItem struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// ListDirs implements fs_list_dirs; mkdir creates a folder inside path first.
func (d *Driver) ListDirs(args map[string]any) ([]protocol.Content, error) {
	path := strings.TrimSpace(protocol.String(args, "path", ""))
	target, err := d.Resolve(args, path)
	if err != nil {
		return nil, err
	}
	mkdir := strings.TrimSpace(protocol.String(args, "mkdir", ""))
	if mkdir != "" {
		created, err := d.Resolve(args, filepath.Join(path, mkdir))
		if err != nil {
			return nil, err
		}
		if err := os.MkdirAll(created, 0o755); err != nil {
			return nil, fmt.Errorf("cannot create %s: %w", mkdir, err)
		}
		target = created
	}
	info, err := os.Stat(target)
	if err != nil {
		return nil, fmt.Errorf("cannot list %s: %w", target, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%s is not a directory", target)
	}
	entries, err := os.ReadDir(target)
	if err != nil {
		return nil, err
	}
	dirs := []ListingItem{}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		if strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		dirs = append(dirs, ListingItem{Name: entry.Name(), Path: filepath.Join(target, entry.Name())})
	}
	sort.Slice(dirs, func(first, second int) bool {
		return strings.ToLower(dirs[first].Name) < strings.ToLower(dirs[second].Name)
	})
	parent := filepath.Dir(target)
	if parent == target {
		parent = ""
	}
	listing := Listing{
		Current:        target,
		Parent:         parent,
		DefaultWorkdir: d.Workdir(args),
		Platform:       runtime.GOOS,
		Roots:          roots(),
		Bookmarks:      bookmarks(d.Workdir(args)),
		Dirs:           dirs,
	}
	payload, err := json.Marshal(listing)
	if err != nil {
		return nil, err
	}
	return []protocol.Content{protocol.Text(string(payload))}, nil
}

func roots() []string {
	home, _ := os.UserHomeDir()
	candidates := []string{home}
	if runtime.GOOS == "windows" {
		for letter := 'A'; letter <= 'Z'; letter++ {
			drive := string(letter) + ":\\"
			if info, err := os.Stat(drive); err == nil && info.IsDir() {
				candidates = append(candidates, drive)
			}
		}
	} else {
		for _, path := range []string{"/", "/mnt", "/media", "/home", "/tmp"} {
			if info, err := os.Stat(path); err == nil && info.IsDir() {
				candidates = append(candidates, path)
			}
		}
	}
	seen := map[string]bool{}
	unique := []string{}
	for _, path := range candidates {
		if path == "" || seen[path] {
			continue
		}
		seen[path] = true
		unique = append(unique, path)
	}
	return unique
}

func bookmarks(workdir string) []Bookmark {
	home, _ := os.UserHomeDir()
	candidates := []Bookmark{{Name: "Workspace", Path: workdir}}
	if home != "" {
		candidates = append(candidates, Bookmark{Name: "Home", Path: home})
		for _, name := range []string{"Desktop", "Documents", "Downloads"} {
			path := filepath.Join(home, name)
			if info, err := os.Stat(path); err == nil && info.IsDir() {
				candidates = append(candidates, Bookmark{Name: name, Path: path})
			}
		}
	}
	seen := map[string]bool{}
	unique := []Bookmark{}
	for _, bookmark := range candidates {
		if bookmark.Path == "" || seen[bookmark.Path] {
			continue
		}
		seen[bookmark.Path] = true
		unique = append(unique, bookmark)
	}
	return unique
}
