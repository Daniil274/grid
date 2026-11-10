package fsdriver

import (
	"gridnode/internal/protocol"
)

func (d *Driver) common() map[string]any {
	return map[string]any{
		"workdir":        protocol.PString("The working folder to stay inside; defaults to the node's workspace."),
		"allow_external": protocol.PBool("Allow a path outside the working folder (default false).", false),
	}
}

func with(base map[string]any, extra map[string]any) map[string]any {
	merged := map[string]any{}
	for key, value := range base {
		merged[key] = value
	}
	for key, value := range extra {
		merged[key] = value
	}
	return merged
}

// Tools is what this driver brings to a node.
func (d *Driver) Tools() []*protocol.Tool {
	common := d.common()
	return []*protocol.Tool{
		{
			Name: "fs_read",
			Description: "Read a text file (with offset and limit_lines), or an image as an image. " +
				"Paths stay inside the working folder unless allow_external is true.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"filepath":    protocol.PString("The file to read, relative to the working folder."),
				"offset":      protocol.PInt("Skip this many lines before returning any (default 0).", 0),
				"limit_lines": protocol.PInt("Return at most this many lines; 0 means all.", 0),
			}), "filepath"),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Read(args)
			},
		},
		{
			Name: "fs_write",
			Description: "Write a whole text file, creating parent folders. Refuses to replace an existing " +
				"file unless overwrite is true.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"filepath":  protocol.PString("The file to write, relative to the working folder."),
				"content":   protocol.PString("The full text of the file."),
				"overwrite": protocol.PBool("Replace the file if it already exists (default false).", false),
			}), "filepath", "content"),
			Annotations: protocol.Run,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Write(args)
			},
		},
		{
			Name: "fs_edit",
			Description: "Apply a unified diff to a file: context lines, several hunks and small offsets are " +
				"understood.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"filepath":      protocol.PString("The file to edit, relative to the working folder."),
				"patch_content": protocol.PString("The unified diff (---/+++/@@ hunks)."),
			}), "filepath", "patch_content"),
			Annotations: protocol.Run,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Edit(args)
			},
		},
		{
			Name:        "fs_append",
			Description: "Append text to the end of a file, starting a new line when the file needs one.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"filepath": protocol.PString("The file to append to, relative to the working folder."),
				"content":  protocol.PString("The text to append."),
			}), "filepath", "content"),
			Annotations: protocol.Run,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Append(args)
			},
		},
		{
			Name:        "fs_glob",
			Description: "Find files by a glob pattern; ** walks recursively. Returns paths relative to directory.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"pattern":     protocol.PString("The glob, e.g. **/*.go or *.md."),
				"directory":   protocol.PStringDefault("The folder to search in, relative to the working folder.", "."),
				"max_results": protocol.PInt("Return at most this many paths (default 100).", 100),
			}), "pattern"),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Glob(args)
			},
		},
		{
			Name: "fs_grep",
			Description: "Search files for a regexp or literal text; skips .git, node_modules and binary files. " +
				"Answers relpath:line: content.",
			InputSchema: protocol.Object(with(common, map[string]any{
				"pattern":         protocol.PString("The text or regular expression to find."),
				"directory":       protocol.PStringDefault("The folder to search in, relative to the working folder.", "."),
				"file_extensions": protocol.PString("Comma-separated extensions to keep, e.g. go,py."),
				"case_sensitive":  protocol.PBool("Match the case exactly (default false).", false),
				"use_regex":       protocol.PBool("Treat pattern as a regular expression (default true).", true),
				"max_results":     protocol.PInt("Return at most this many matches (default 100).", 100),
			}), "pattern"),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.Grep(args)
			},
		},
		{
			Name: "fs_list_dirs",
			Description: "List the folders of the device for a folder picker: the current folder, its parent, " +
				"bookmarks, drives or root folders, and its subfolders. mkdir creates a subfolder first.",
			InputSchema: protocol.Object(map[string]any{
				"path":    protocol.PString("The folder to list; defaults to the working folder."),
				"mkdir":   protocol.PString("Create this subfolder of path, then list it."),
				"workdir": common["workdir"],
			}),
			Annotations: protocol.Look,
			Handler: func(ctx protocol.Context, args map[string]any) ([]protocol.Content, error) {
				return d.ListDirs(args)
			},
		},
	}
}
