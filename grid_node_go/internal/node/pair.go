package node

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"gridnode/internal/state"
)

// Pair trades a one-time code for this device's token and saves it (0600).
func Pair(ctx context.Context, server, code, name, kind, stateDir string) (state.Token, string, error) {
	server = strings.TrimRight(strings.TrimSpace(server), "/")
	if server == "" {
		return state.Token{}, "", fmt.Errorf("--server is required to pair")
	}
	code = strings.TrimSpace(code)
	if code == "" {
		return state.Token{}, "", fmt.Errorf("--pair needs the code from the web chat's Devices page")
	}
	if kind == "" {
		kind = "desktop"
	}
	payload, err := json.Marshal(map[string]string{"code": code, "name": name, "kind": kind})
	if err != nil {
		return state.Token{}, "", err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, server+"/node/pair", bytes.NewReader(payload))
	if err != nil {
		return state.Token{}, "", err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", "grid-node/"+Version)
	client := &http.Client{Timeout: 30 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return state.Token{}, "", fmt.Errorf("cannot reach %s: %w", server, err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
	if response.StatusCode != http.StatusOK {
		detail := strings.TrimSpace(string(body))
		var problem struct {
			Detail string `json:"detail"`
		}
		if json.Unmarshal(body, &problem) == nil && problem.Detail != "" {
			detail = problem.Detail
		}
		return state.Token{}, "", fmt.Errorf("pairing failed: HTTP %d %s", response.StatusCode, detail)
	}
	var paired struct {
		Device string `json:"device"`
		Token  string `json:"token"`
	}
	if err := json.Unmarshal(body, &paired); err != nil {
		return state.Token{}, "", fmt.Errorf("pairing answered something unexpected: %w", err)
	}
	if paired.Token == "" {
		return state.Token{}, "", fmt.Errorf("pairing answered without a token")
	}
	token := state.Token{Server: server, Device: paired.Device, Token: paired.Token}
	path, err := state.SaveToken(stateDir, token)
	if err != nil {
		return state.Token{}, "", fmt.Errorf("paired, but the token could not be saved in %s: %w", stateDir, err)
	}
	return token, path, nil
}
