package toolrunner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestARuntimeDownloadIsHeldAgainstItsPublishedSha walks the three sides of the
// promise: a matching sha passes, a tampered download never runs, and a server
// from before the sidecar existed still serves.
func TestARuntimeDownloadIsHeldAgainstItsPublishedSha(t *testing.T) {
	var archive, sidecar []byte
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/node/runtime/python-linux-amd64.tar.gz":
			_, _ = writer.Write(archive)
		case "/node/runtime/python-linux-amd64.tar.gz.sha256":
			if len(sidecar) == 0 {
				http.NotFound(writer, request)
				return
			}
			_, _ = writer.Write(sidecar)
		default:
			http.NotFound(writer, request)
		}
	}))
	defer server.Close()
	runner := New(t.TempDir(), server.URL, "secret", t.TempDir())
	url := server.URL + "/node/runtime/python-linux-amd64.tar.gz"

	good := []byte("the interpreter archive")
	sum := sha256.Sum256(good)
	archive, sidecar = good, []byte(hex.EncodeToString(sum[:]))
	data, err := runner.downloadVerified(context.Background(), url)
	if err != nil || string(data) != string(good) {
		t.Fatalf("the matching download failed: %v (%q)", err, data)
	}

	archive = []byte("another archive entirely")
	_, err = runner.downloadVerified(context.Background(), url)
	if err == nil || !strings.Contains(err.Error(), "its .sha256 publishes") {
		t.Fatalf("a tampered download must not pass: %v", err)
	}

	archive, sidecar = good, nil
	data, err = runner.downloadVerified(context.Background(), url)
	if err != nil || string(data) != string(good) {
		t.Fatalf("a sidecar-less download failed: %v (%q)", err, data)
	}
}
