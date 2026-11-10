package fsdriver

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Hunk is one "@@ -a,b +c,d @@" block of a unified diff.
type Hunk struct {
	OldStart int
	OldCount int
	NewStart int
	NewCount int
	Ops      []Op
}

// Op is one line of a hunk: ' ' context, '-' removed, '+' added.
type Op struct {
	Kind      byte
	Text      string
	NoNewline bool
}

var hunkHeader = regexp.MustCompile(`^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@+`)

// ParseUnifiedDiff reads the hunks of a unified diff. File headers, index
// lines, git extras and "\ No newline at end of file" markers are understood.
func ParseUnifiedDiff(patch string) ([]Hunk, error) {
	normalized := strings.ReplaceAll(patch, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	hunks := []Hunk{}
	var current *Hunk
	for index := 0; index < len(lines); index++ {
		line := lines[index]
		if match := hunkHeader.FindStringSubmatch(line); match != nil {
			hunk := Hunk{}
			hunk.OldStart = atoiDefault(match[1], 0)
			hunk.OldCount = atoiDefault(match[2], 1)
			hunk.NewStart = atoiDefault(match[3], 0)
			hunk.NewCount = atoiDefault(match[4], 1)
			hunks = append(hunks, hunk)
			current = &hunks[len(hunks)-1]
			continue
		}
		if current == nil {
			// Header of the diff itself (-–/+++/diff/index/rename...): skip.
			continue
		}
		if line == `\ No newline at end of file` {
			if len(current.Ops) > 0 {
				current.Ops[len(current.Ops)-1].NoNewline = true
			}
			continue
		}
		if strings.HasPrefix(line, "@@") {
			continue
		}
		if line == "" {
			// A trailing blank line inside a hunk is an empty context line.
			current.Ops = append(current.Ops, Op{Kind: ' '})
			continue
		}
		kind := line[0]
		switch kind {
		case ' ', '-', '+':
			current.Ops = append(current.Ops, Op{Kind: kind, Text: line[1:]})
		default:
			return nil, fmt.Errorf("bad line in hunk %d: %q", len(hunks), truncate(line, 80))
		}
	}
	if len(hunks) == 0 {
		return nil, fmt.Errorf("no hunks found in patch")
	}
	for index := range hunks {
		hunks[index].OldCount = countOld(hunks[index])
		hunks[index].NewCount = countNew(hunks[index])
	}
	// A hunk header may say 0 lines for an empty side; keep what the ops say.
	return hunks, nil
}

func countOld(hunk Hunk) int {
	total := 0
	for _, op := range hunk.Ops {
		if op.Kind == ' ' || op.Kind == '-' {
			total++
		}
	}
	return total
}

func countNew(hunk Hunk) int {
	total := 0
	for _, op := range hunk.Ops {
		if op.Kind == ' ' || op.Kind == '+' {
			total++
		}
	}
	return total
}

func atoiDefault(value string, def int) int {
	if value == "" {
		return def
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return def
	}
	return parsed
}

func truncate(text string, size int) string {
	if len(text) <= size {
		return text
	}
	return text[:size] + "…"
}

func splitLines(text string) (lines []string, finalNewline bool) {
	if text == "" {
		return nil, false
	}
	finalNewline = strings.HasSuffix(text, "\n")
	if finalNewline {
		text = text[:len(text)-1]
	}
	return strings.Split(text, "\n"), finalNewline
}

// ApplyUnifiedDiff applies every hunk of patch to original and returns the new
// text with the number of hunks applied. Matching tolerates offsets and trims
// up to three context lines per side when a hunk does not fit exactly.
func ApplyUnifiedDiff(original, patch string) (string, int, error) {
	hunks, err := ParseUnifiedDiff(patch)
	if err != nil {
		return "", 0, err
	}
	lines, finalNewline := splitLines(original)
	cursor := 0
	for number, hunk := range hunks {
		applied, next, err := applyHunk(lines, cursor, hunk)
		if err != nil {
			return "", 0, fmt.Errorf("hunk %d (%s): %w", number+1, headerOf(hunk), err)
		}
		lines = applied
		cursor = next
	}
	result := strings.Join(lines, "\n")
	if len(lines) > 0 && finalNewline {
		result += "\n"
	}
	return result, len(hunks), nil
}

func headerOf(hunk Hunk) string {
	return fmt.Sprintf("@@ -%d,%d +%d,%d @@", hunk.OldStart, hunk.OldCount, hunk.NewStart, hunk.NewCount)
}

// oldBlock is the lines a hunk expects to find.
func oldBlock(ops []Op) []string {
	block := make([]string, 0, len(ops))
	for _, op := range ops {
		if op.Kind == ' ' || op.Kind == '-' {
			block = append(block, op.Text)
		}
	}
	return block
}

func matchesAt(lines []string, position int, block []string) bool {
	if position < 0 || position+len(block) > len(lines) {
		return false
	}
	for index, expected := range block {
		if lines[position+index] != expected {
			return false
		}
	}
	return true
}

func applyHunk(lines []string, cursor int, hunk Hunk) ([]string, int, error) {
	expected := hunk.OldStart - 1
	if expected < cursor {
		expected = cursor
	}
	ops := hunk.Ops
	for fuzz := 0; fuzz <= 3; fuzz++ {
		trimmed := trimContext(ops, fuzz)
		block := oldBlock(trimmed)
		position, ok := locate(lines, cursor, expected, block)
		if !ok {
			continue
		}
		// Reapply the trimmed ops at position.
		result := make([]string, 0, len(lines)+len(trimmed))
		result = append(result, lines[:position]...)
		for _, op := range trimmed {
			switch op.Kind {
			case ' ':
				result = append(result, op.Text)
			case '-':
				// dropped
			case '+':
				result = append(result, op.Text)
			}
		}
		result = append(result, lines[position+len(block):]...)
		return result, position + len(block), nil
	}
	return nil, 0, fmt.Errorf("the patch does not match the file (expected around line %d)", hunk.OldStart)
}

// trimContext drops up to count leading and trailing context lines.
func trimContext(ops []Op, count int) []Op {
	start := 0
	end := len(ops)
	for removed := 0; removed < count && start < end && ops[start].Kind == ' '; removed++ {
		start++
	}
	for removed := 0; removed < count && end > start && ops[end-1].Kind == ' '; removed++ {
		end--
	}
	return ops[start:end]
}

// locate finds the block at the expected line, then at the cursor, then onward.
func locate(lines []string, cursor, expected int, block []string) (int, bool) {
	if len(block) == 0 {
		if expected > len(lines) {
			expected = len(lines)
		}
		return expected, true
	}
	if expected >= cursor && matchesAt(lines, expected, block) {
		return expected, true
	}
	if cursor >= 0 && cursor < len(lines) && matchesAt(lines, cursor, block) {
		return cursor, true
	}
	for position := cursor; position+len(block) <= len(lines); position++ {
		if matchesAt(lines, position, block) {
			return position, true
		}
	}
	return 0, false
}
